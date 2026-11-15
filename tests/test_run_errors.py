"""An unexpected error costs only the question or the shop it happens on: the run goes on and writes every other answer."""

import json

import datamate.agent.pipeline as pipeline
from datamate.agent.pipeline import RunConfig, run
from datamate.rules import TRAIN_RULEBOOK


def split(tmp_path):
    root = tmp_path / "root"
    test, train = root / "test", root / "train"
    for d in (test, train):
        d.mkdir(parents=True)
        (d / "calendar.csv").write_text("date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n"
                                        "2025-01-01,3,2,12,2024,false,\n", encoding="utf-8")
    (test / "rulebook.md").write_text(TRAIN_RULEBOOK.read_text(encoding="utf-8"), encoding="utf-8")
    qs = []
    for biz in ("shop_a", "shop_b"):
        (test / "data" / biz).mkdir(parents=True)
        (test / "data" / biz / "README.md").write_text("Sổ bán hàng. Dữ liệu có đến hết 31/01/2025.\n", encoding="utf-8")
        (test / "data" / biz / "ban_hang.csv").write_text(
            "Mã đơn,Ngày,Tên hàng,Số lượng,Đơn giá,Thành tiền\n"
            + "".join(f"HD{i},{i % 28 + 1:02d}/01/2025,Trà sữa,1,30000,30000\n" for i in range(20)), encoding="utf-8")
        qs += [{"id": f"q_{biz}_{i}", "business_id": biz, "question": "Doanh thu tháng 1/2025?", "as_of_date": "2025-01-31"}
               for i in range(3)]
    (test / "questions.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qs), encoding="utf-8")
    (train / "questions.jsonl").write_text(json.dumps(qs[0], ensure_ascii=False) + "\n", encoding="utf-8")
    (train / "answers.jsonl").write_text(json.dumps({"id": qs[0]["id"], "type": "number", "answer": 1}) + "\n",
                                         encoding="utf-8")
    return root


def test_a_failing_question_and_a_failing_shop_abstain_and_the_run_goes_on(tmp_path, monkeypatch):
    root = split(tmp_path)

    def solve(q, *a, **k):
        if q["id"] == "q_shop_b_1":
            raise KeyError("a data shape no rule expected")
        return {**q, "final": {"type": "number", "answer": 1}, "samples": [], "elapsed": 0.01}

    real_audit = pipeline.audit

    def audit(biz, *a, **k):
        if biz.id == "shop_a":
            raise ValueError("an unreadable shop")
        return real_audit(biz, *a, **k)

    monkeypatch.setattr(pipeline, "solve_question", solve)
    monkeypatch.setattr(pipeline, "audit", audit)
    out = tmp_path / "out"
    run(RunConfig(split="test", data_root=root, out_dir=out, cache_dir=tmp_path / "cache" / "ingest", backend="dummy",
                  engine="sql", examples_path=None, catalog="delta", context="rich", slots="none"))
    sub = {r["id"]: r for r in map(json.loads, (out / "submission.jsonl").read_text(encoding="utf-8").splitlines())}
    assert {i: r["type"] for i, r in sub.items()} == {"q_shop_a_0": "abstain", "q_shop_a_1": "abstain", "q_shop_a_2": "abstain",
                                                      "q_shop_b_0": "number", "q_shop_b_1": "abstain", "q_shop_b_2": "number"}
    log = json.loads((out / "logs" / "q_shop_b_1.json").read_text(encoding="utf-8"))
    assert log["answer"] == {"type": "abstain"} and log["error"].startswith("question: KeyError") and "Traceback" in log["traceback"]
    assert json.loads((out / "logs" / "q_shop_a_0.json").read_text(encoding="utf-8"))["error"].startswith("shop setup: ValueError")
