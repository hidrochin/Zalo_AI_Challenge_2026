"""Stress test for unseen shops: change train shops on purpose, then measure what ingestion still recovers.

Each mode changes one thing the private test could change (headers, README, cell formats, status words, name spellings,
subtotal rows), so a drop points at one weakness. The contracts of eval/semantic.py are the yardstick: they have gold
answers, and a mutation changes how a shop is written, not what it sold. Modes that change values and rulebook variants
(rules.py) are reported apart, because they measure different abilities.
"""

import random
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import pandas as pd

from datamate.agent.columns import propose_roles
from datamate.agent.statuses import ask_labels
from datamate.canon import build_sales
from datamate.eval.semantic import contract_items, empty_report, score_contracts
from datamate.ingest import load_calendar
from datamate.ingest.business import Business, apply_status, assemble, extract_pieces, read_sheets
from datamate.ingest.parsers import _is_null, parse_date, parse_number
from datamate.ingest.readme import Readme, parse_readme
from datamate.ingest.roles import assign_roles, normalize_status
from datamate.ingest.tables import RawSheet, detect_header
from datamate.rules import rule_gate
from datamate.text import fold

# Headers no rule in ingest/roles.py knows.
UNSEEN = {"order_id": "Số bill", "order_date": "Ngày chứng từ", "order_ts": "Mốc tạo bill", "status": "Tình hình xử lý",
          "product_id": "Mã vật tư", "product_name": "Diễn giải", "qty": "Lượng xuất", "qty_base": "Lượng quy đổi",
          "unit": "Cách tính", "base_unit": "Đơn vị gốc", "unit_price": "Giá niêm yết", "line_amount": "Doanh số dòng",
          "customer_id": "Mã người mua", "customer_name": "Người mua"}
# Status words the rules do not know, by rulebook status.
STATUS_WORDS = {"hoàn thành": "Chốt đơn xong", "huỷ": "Khách bom hàng", "trả hàng": "Khách gửi lại đồ", "đang giao": "Shipper đang cầm"}
SUBTOTAL_LABEL = "Cộng phát sinh {:02d}/{:02d}"

Pieces = list[tuple[RawSheet, pd.DataFrame]]


def _roles(sh: RawSheet, df: pd.DataFrame, readme: Readme) -> dict[str, str]:
    return assign_roles(list(df.columns), readme.descs_for(sh.file, sh.sheet), readme.order_keys)


def _map_role(pieces: Pieces, readme: Readme, roles: tuple, fn) -> Pieces:
    """Apply fn to the cells of the columns with these roles."""
    out = []
    for sh, df in pieces:
        df = df.copy()
        for c, r in _roles(sh, df, readme).items():
            if r in roles:
                df[c] = df[c].map(lambda v: v if _is_null(v) else fn(v))
        out.append((sh, df))
    return out


def untouched(pieces: Pieces, readme: Readme, rng: random.Random):
    """With model steps on, shows what they change on a shop the rules already read."""
    return pieces, readme, {}


def shuffle(pieces: Pieces, readme: Readme, rng: random.Random):
    out = []
    for sh, df in pieces:
        cols = list(df.columns)
        rng.shuffle(cols)
        out.append((sh, df[cols]))
    return out, readme, {}


def no_readme(pieces: Pieces, readme: Readme, rng: random.Random):
    return pieces, Readme("", readme.data_end), {}


def rename_headers(pieces: Pieces, readme: Readme):
    """Headers of the mapped columns become UNSEEN ones; README sections follow."""
    out, renamed = [], {}
    for sh, df in pieces:
        new = {c: UNSEEN[r] for c, r in _roles(sh, df, readme).items() if r in UNSEEN}
        renamed.update(new)
        out.append((sh, df.rename(columns=new)))
    columns = {k: {renamed.get(c, c): d for c, d in cols.items()} for k, cols in readme.columns.items()}
    folded = {fold(a): fold(b) for a, b in renamed.items()}
    return out, replace(readme, columns=columns, order_keys={folded.get(k, k) for k in readme.order_keys}), renamed


def rename(pieces: Pieces, readme: Readme, rng: random.Random):
    return rename_headers(pieces, readme)


def rename_no_readme(pieces: Pieces, readme: Readme, rng: random.Random):
    pieces, readme, renamed = rename_headers(pieces, readme)
    return pieces, Readme("", readme.data_end), renamed


def _date_text(v):
    if isinstance(v, str) and "/" in v and v.count("/") == 1:  # no year in the cell: the sheet gives it
        return v
    d = parse_date(v, 2000, True)
    return v if d is None else f"{d.day} thg {d.month} {d.year}"


def _money_text(v):
    n = parse_number(v)
    return v if n is None or n != int(n) else f"{int(n):,}".replace(",", ".") + "đ"


def formats(pieces: Pieces, readme: Readme, rng: random.Random):
    pieces = _map_role(pieces, readme, ("order_date",), _date_text)
    return _map_role(pieces, readme, ("line_amount", "unit_price"), _money_text), readme, {}


def status_words(pieces: Pieces, readme: Readme, rng: random.Random):
    return _map_role(pieces, readme, ("status",), lambda v: STATUS_WORDS.get(normalize_status(v), v)), readme, {}


def name_spellings(pieces: Pieces, readme: Readme, rng: random.Random):
    """A third of the rows lose accents and case in their names, as in a second person's notebook. Keyed on the row,
    so the identical rows of an export bug stay identical."""
    out = []
    for sh, df in pieces:
        df = df.copy()
        cols = [c for c, r in _roles(sh, df, readme).items() if r in ("product_name", "customer_name")]
        hit = [random.Random(str(row)).random() < 0.33 for row in df.itertuples(index=False, name=None)]
        for c in cols:
            df.loc[hit, c] = df.loc[hit, c].map(lambda v: fold(v).upper() if isinstance(v, str) else v)
        out.append((sh, df))
    return out, readme, {}


def subtotal_rows(sheets: list[RawSheet], readme: Readme, rng: random.Random, every: int = 25) -> list[RawSheet]:
    """Rows with a label the rules do not know and the block's total in the amount column."""
    out = []
    for sh in sheets:
        h = None if sh.has_header else detect_header(sh.df, readme.known_names())
        header = [] if h is None else ["" if _is_null(v) else str(v).strip() for v in sh.df.iloc[h].tolist()]
        roles = assign_roles(header, readme.descs_for(sh.file, sh.sheet), readme.order_keys)
        amount = next((header.index(c) for c, r in roles.items() if r == "line_amount"), None)
        if amount is None or len(sh.df) - h <= every:
            out.append(sh)
            continue
        rows, total = [], 0.0
        for i, row in enumerate(sh.df.itertuples(index=False, name=None)):
            rows.append(row)
            total += (parse_number(row[amount]) or 0.0) if i > h else 0.0
            if i > h and (i - h) % every == 0:
                extra = [None] * len(row)
                extra[0 if amount else 1] = SUBTOTAL_LABEL.format(rng.randint(1, 28), rng.randint(1, 12))
                extra[amount] = total
                rows.append(tuple(extra))
                total = 0.0
        out.append(replace(sh, df=pd.DataFrame(rows, columns=sh.df.columns)))
    return out


PIECE_MODES = {"untouched": untouched, "shuffle": shuffle, "no_readme": no_readme, "rename": rename, "rename_no_readme": rename_no_readme,
               "formats": formats, "status_words": status_words, "name_spellings": name_spellings}
MODES = [*PIECE_MODES, "subtotal_rows"]


def mutated(biz_id: str, readme: Readme, sheets: list[RawSheet], pieces: Pieces, mode: str, seed: int):
    """(shop, renamed columns, rebuild(extra_roles) -> shop)."""
    rng = random.Random(f"{seed}:{biz_id}:{mode}")
    warn: list[str] = []
    renamed: dict = {}
    if mode == "subtotal_rows":
        new_pieces, new_readme = extract_pieces(subtotal_rows(sheets, readme, rng), readme.known_names(), warn), readme
    else:
        new_pieces, new_readme, renamed = PIECE_MODES[mode](pieces, readme, rng)

    def rebuild(extra_roles: dict | None = None) -> Business:
        return assemble(biz_id, "train", new_readme, new_pieces, list(warn), extra_roles=extra_roles)
    return rebuild(), renamed, rebuild


def model_steps(generate):
    """The ingest steps that call the model, for shops held in memory."""
    def after(biz: Business, rebuild) -> Business:
        roles = propose_roles(biz, generate)
        biz = rebuild(roles) if roles else biz
        labels = ask_labels(biz, generate)
        if labels:
            apply_status(biz, labels)
        return biz
    return after


def recovered_roles(base: Business, biz: Business, renamed: dict) -> Counter:
    """Per role: columns that kept it ('role') and columns that lost it ('role!') or got another ('role?')."""
    c: Counter = Counter()
    for name, ti in base.info.items():
        now = biz.info[name].roles if name in biz.info else {}
        for col, role in ti.roles.items():
            got = now.get(renamed.get(col, col))
            c[role if got == role else f"{role}!" if got is None else f"{role}?"] += 1
    return c


def _entities(biz: Business) -> dict[str, int]:
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
    return {k: int(sales[k].nunique()) for k in ("product_key", "customer_key") if sales is not None and k in sales.columns}


def run_mutations(data_root: Path, modes: list[str] | None = None, shops: list[str] | None = None, seed: int = 0, after=None) -> dict:
    """after(biz, rebuild) -> biz runs on each mutated shop, so the model steps can be measured against the plain rules."""
    train = Path(data_root) / "train"
    cal = load_calendar(train)
    checks, todo, gold = contract_items(train)
    reps = {m: empty_report(checks) for m in ["baseline", *(modes or MODES)]}
    roles: dict[str, Counter] = defaultdict(Counter)
    entities: dict[str, Counter] = defaultdict(Counter)
    for biz_id in sorted(shops or todo):
        bdir = train / "data" / biz_id
        readme = parse_readme((bdir / "README.md").read_text(encoding="utf-8") if (bdir / "README.md").exists() else "")
        warn: list[str] = []
        sheets = read_sheets(bdir, warn)
        pieces = extract_pieces(sheets, readme.known_names(), warn)
        base = assemble(biz_id, "train", readme, pieces, warn)
        score_contracts(base, todo[biz_id], cal, gold, reps["baseline"])
        ents = _entities(base)
        entities["baseline"].update(ents)
        for mode in reps:
            if mode == "baseline":
                continue
            biz, renamed, rebuild = mutated(biz_id, readme, sheets, pieces, mode, seed)
            biz = after(biz, rebuild) if after else biz
            roles[mode].update(recovered_roles(base, biz, renamed))
            score_contracts(biz, todo[biz_id], cal, gold, reps[mode], max_misses=2)
            entities[mode].update(_entities(biz))
        print(f"{biz_id} done", flush=True)
    out = {}
    for mode, rep in reps.items():
        kept = sum(n for r, n in roles[mode].items() if r[-1] not in "!?")
        total = sum(roles[mode].values())
        out[mode] = {"exact": sum(r["exact"] for r in rep.values()), "matched": sum(r["matched"] for r in rep.values()),
                     "by_check": {k: [r["exact"], r["matched"]] for k, r in rep.items()},
                     "skipped": dict(sum((Counter(r["skipped"]) for r in rep.values()), Counter())),
                     "roles_kept": [kept, total], "lost": {r: n for r, n in roles[mode].most_common() if r[-1] in "!?"},
                     "entities": dict(entities[mode]), "misses": [m for r in rep.values() for m in r["misses"]][:6]}
    return out


# (clause, new text or None to delete it, does a number change?)
RULE_VARIANTS = [
    ("Đơn tính vào doanh thu", "đơn **không huỷ**. Đơn trả một phần vẫn tính phần còn lại; đơn đang giao chưa tính", True),
    ("Doanh thu một đơn", "tổng tiền cả đơn sau giảm giá trên đơn, đã gồm phí giao hàng", True),
    ("Đơn thuộc kỳ nào", "theo **ngày giao hàng**, không theo ngày đặt", True),
    ("Số đơn", "tính mọi đơn đã đặt, kể cả đơn huỷ và đơn trả", True),
    ("Đơn bị huỷ", "đơn có trạng thái huỷ hoặc trả hàng", True),
    ("Tỉ lệ đơn huỷ trong kỳ", "phần trăm đơn huỷ trong các đơn đã hoàn thành của kỳ", True),
    ("Giá trị trung bình một đơn", "làm tròn xuống đến nghìn đồng", True),
    ("Đơn tính vào doanh thu", None, True),
    ("Đơn thuộc kỳ nào", "theo **ngày đặt hàng**; ngày giao và ngày thu tiền không dùng để xếp đơn vào kỳ", False),
    ("Đơn bị huỷ", "đơn mang trạng thái huỷ. Đơn trả hàng thì **không** được xem là đơn huỷ", False),
]


def run_rule_variants(rulebook: str, cache_dir: Path, generate) -> dict:
    """Does the gate turn a definition off for each changed clause, and leave it on for each rewording?"""
    rows = []
    for i, (name, text, changed) in enumerate(RULE_VARIANTS):
        out = []
        for line in rulebook.splitlines():
            if line.startswith(f"| {name} |"):
                line = None if text is None else f"| {name} | {text} |"
            if line is not None:
                out.append(line)
        off = rule_gate("\n".join(out), Path(cache_dir) / f"variant{i}.json", generate)
        rows.append({"clause": name, "changed": changed, "off": sorted(off), "ok": bool(off) == changed})
    return {"caught": [sum(r["ok"] for r in rows if r["changed"]), sum(r["changed"] for r in rows)],
            "kept": [sum(r["ok"] for r in rows if not r["changed"]), sum(not r["changed"] for r in rows)], "rows": rows}
