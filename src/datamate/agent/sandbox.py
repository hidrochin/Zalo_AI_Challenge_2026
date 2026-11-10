"""Execute generated programs in worker processes that hold one business's tables in memory."""

import contextlib
import io
import json
import multiprocessing as mp
import queue
import re
import signal
import traceback
from pathlib import Path

_CTX = mp.get_context("spawn")


def _short_tb() -> str:
    lines = traceback.format_exc().splitlines()
    keep = [ln for i, ln in enumerate(lines) if "<program>" in ln or (i > 0 and "<program>" in lines[i - 1])]
    tail = lines[-3:]
    return "\n".join(keep[-6:] + [ln for ln in tail if ln not in keep])[-2000:]


_DM_IMPORT = re.compile(r"^[ \t]*(from[ \t]+datamate[\w.]*[ \t]+import[^\n]*|import[ \t]+datamate[^\n]*)$", re.M)


def _compile(code: str):
    """Forgive harmless slips: `datamate` imports (helpers are pre-loaded) and top-level `return`."""
    code = _DM_IMPORT.sub("pass", code)
    try:
        return compile(code, "<program>", "exec")
    except SyntaxError as e:
        if "outside function" not in str(e):
            raise
        body = "\n".join("    " + ln for ln in code.splitlines())
        return compile(f"def __program_main():\n{body}\n__program_main()\n", "<program>", "exec")


_EMPTY_REASON = re.compile(r"khong (tim thay|co (don|dong|giao dich|du lieu ban|ban|ghi nhan|ban ghi))|no (sales|rows?|records?|orders?|data|transactions?)( |$)"
                           r"|not found|empty|chua co (don|giao dich)|khong ban duoc")
_HELPER_REASON = re.compile(r"sales_lines|orders\(\)|helper|use t directly|compute from t|not available for this shop")
_BOOLISH = {"true", "false", "none", "nan", "null", ""}


def name_index(tables, sales=None) -> set[str]:
    """norm_key of every text value in the data: a string answer must be one of these (a name or a code)."""
    import pandas as pd

    from datamate.canon import strip_phone
    from datamate.text import norm_key

    out: set[str] = set()
    frames = list(tables.values()) + ([sales] if sales is not None else [])
    for df in frames:
        for c in df.columns:
            if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
                u = df[c].dropna().unique()
                if len(u) <= 200_000:
                    out.update(norm_key(v) for v in u)
                    out.update(norm_key(strip_phone(v)) for v in u if isinstance(v, str) and v[-1:].isdigit() or str(v).endswith(")"))
    return out


def check_result(result: dict, names: set[str] | None, helper_guard: bool = True, outside: bool = False,
                 zero_ok: bool = False) -> tuple[str | None, str | None]:
    """(hard error, soft warning) for an executed answer; hard errors mean the answer is not usable.
    helper_guard: refuse an unanswerable blamed on a missing helper (pandas; in SQL the reason names views as a matter of course).
    outside: every period the question names lies outside the data, so 'no rows' is a valid reason (rulebook).
    zero_ok: the answer comes straight from a macro tested on gold answers, so 0 is not a filter bug."""
    from datamate.text import fold, norm_key

    t, a = result.get("type"), result.get("answer")

    def bad_name(v) -> str | None:
        if isinstance(v, bool) or (isinstance(v, str) and v.strip().lower() in _BOOLISH):
            return f"{v!r} is a flag, not a name"
        if isinstance(v, str) and names is not None and norm_key(v) not in names:
            return f"{v!r} does not appear anywhere in the data"
        return None

    if t == "string":
        why = bad_name(a)
        if why:
            return (f"answer_string: {why}. Return a name (or code) copied from the data, e.g. product_name or customer_name; "
                    "if the data cannot answer, call unanswerable(reason) instead of explaining in a string."), None
    if t == "list":
        for v in a:
            why = bad_name(v) if isinstance(v, (str, bool)) else None
            if why:
                return f"answer_list item {why}. Items must be names/codes copied from the data, or numbers.", None
    if t == "table":
        for r in a.get("rows", []):
            why = bad_name(r[0]) if r and isinstance(r[0], (str, bool)) else None
            if why:
                return f"answer_table first column: {why}.", None
    if helper_guard and t == "unanswerable" and result.get("_src") == "model" and _HELPER_REASON.search(fold(result.get("reason", ""))):
        return ("You called unanswerable() because a helper (sales_lines()/orders()) is not available. That is not a reason: "
                "the data is still in T. Compute the answer from the T tables listed in the schema card. "
                "Only call unanswerable() when a needed table/column does not exist or the period is outside the data."), None
    if not outside and t == "unanswerable" and result.get("_src") == "model" and _EMPTY_REASON.search(fold(result.get("reason", ""))):
        return ("You called unanswerable() because your filter found no rows. An empty result here almost always means a bug "
                "(wrong year, wrong column, exact string match instead of find(), date comparison). Print the data you filter on and fix it. "
                "Only call unanswerable() when a needed table/column does not exist or the period is outside the data."), None
    if t in ("number", "percent") and isinstance(a, (int, float)) and not isinstance(a, bool):
        if a == 0 and not zero_ok:
            return None, ("The answer is 0. If the shop has data in this period, 0 usually means a filter bug "
                          "(wrong column, wrong year, case-sensitive match, a shift/flag that never matches). Double-check; keep 0 only if it is really 0.")
        if t == "percent" and abs(a) > 1000:
            return None, f"The percent is {a}, which is implausible. Check the base period and that you passed percent units."
    return None, None


def shadow_check(result: dict, shadow: tuple[str, str] | None) -> tuple[str | None, dict | None]:
    """Semantic validation: when the question is one of the semantic-layer contracts (eval/semantic.py, exact on
    train gold), recompute it with the primitive and flag a disagreeing answer. dm context must be set.
    Returns (warning, the primitive's answer)."""
    if not shadow or result.get("type") not in ("number", "percent"):
        return None, None
    from datamate.eval.semantic import find_check, shadow_value

    text, period_call = shadow
    check = find_check(text, result["type"])
    if check is None:
        return None, None
    try:
        value = shadow_value(check, period_call)
    except Exception:  # the primitive cannot compute it here: no opinion
        return None, None
    if abs(float(value) - float(result.get("answer", 0))) <= 1e-6:
        return None, None
    call = {"avg_order_value": f"avg_order_value({period_call})", "revenue": f"revenue({period_call})"}.get(
        check.name, f"orders() filtered with in_period(o['date'], {period_call})")
    warning = (f"Your answer disagrees with the rulebook definition of '{check.name}' for {period_call}. "
               f"Recompute it with {call} (it applies the default counting rules) unless the question asks for something else.")
    return warning, {"type": result["type"], "answer": value}


def _execute(code: str, as_of: str, tables, cal, timeout: int, sales=None, names=None, shadow=None, facts=None) -> dict:
    import numpy as np
    import pandas as pd

    from datamate import dm

    dm.set_context(as_of, cal, sales, facts)
    ns = {"__name__": "__program__", "pd": pd, "np": np, "dm": dm, "AS_OF": dm.AS_OF, "CAL": cal,
          "T": {k: v.copy(deep=False) for k, v in tables.items()}}
    ns.update({n: getattr(dm, n) for n in dm.__all__})
    buf = io.StringIO()
    result, error = None, None
    signal.alarm(timeout)
    try:
        with contextlib.redirect_stdout(buf):
            exec(_compile(code), ns)
            if dm._RESULT is None and callable(ns.get("main")):  # defined main() but never called it
                ns["main"]()
        result = dm._RESULT
        if result is None:
            error = "The program finished without calling an answer_* function or unanswerable()."
    except dm.Unanswerable as e:
        result = {"type": "unanswerable", "reason": str(e), "_src": "guard"}
    except BaseException:
        error = _short_tb()
    finally:
        signal.alarm(0)
    warning = contract = None
    if result is not None:
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as e:
            result, error = None, f"answer is not JSON-serializable: {e}"
    if result is not None and error is None:
        error, warning = check_result(result, names)
    if result is not None and error is None and warning is None:
        dm.set_context(as_of, cal, sales, facts)  # the program may have changed module state
        warning, contract = shadow_check(result, shadow)
    return {"result": result if error is None else None, "error": error, "warning": warning, "contract": contract, "stdout": buf.getvalue()[-3000:]}


def _limit_memory(extra_gb: float = 4.0) -> None:
    """Cap the worker's address space at its current size + extra_gb (Linux), so a runaway program (e.g. a huge merge)
    gets a MemoryError instead of pushing the host into the OOM killer, which may kill the model server."""
    import os
    import resource

    try:
        with open("/proc/self/statm") as f:
            vm_bytes = int(f.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
        cap = vm_bytes + int(float(os.environ.get("DM_WORKER_MEM_GB", extra_gb)) * 2**30)
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except (OSError, ValueError):  # no /proc (macOS) or limit not allowed: run without a cap
        pass


def _worker(conn, split_dir: str, biz_id: str, cache_dir: str | None, timeout: int) -> None:
    from datamate.canon import build_sales
    from datamate.facts import build_facts
    from datamate.ingest import load_business, load_calendar

    biz = load_business(Path(split_dir), biz_id, Path(cache_dir) if cache_dir else None)
    cal = load_calendar(Path(split_dir))
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})
    names = name_index(biz.tables, sales)
    facts = build_facts(biz, sales)
    _limit_memory()

    def on_alarm(signum, frame):
        raise TimeoutError(f"program exceeded {timeout}s")

    signal.signal(signal.SIGALRM, on_alarm)
    conn.send("ready")
    while True:
        msg = conn.recv()
        if msg is None:
            break
        code, as_of, shadow = msg
        conn.send(_execute(code, as_of, biz.tables, cal, timeout, sales, names, shadow, facts))


class ExecPool:
    """N worker processes for one business; run() is thread-safe."""

    def __init__(self, split_dir: Path, biz_id: str, cache_dir: Path | None, n: int = 2, timeout: int = 30):
        self.args = (str(split_dir), biz_id, str(cache_dir) if cache_dir else None, timeout)
        self.timeout = timeout
        self._free: queue.Queue = queue.Queue()
        self._all = []
        for _ in range(max(1, n)):
            self._free.put(self._start())

    def _start(self):
        parent, child = _CTX.Pipe()
        p = _CTX.Process(target=_worker, args=(child, *self.args), daemon=True)
        p.start()
        if not parent.poll(900) or parent.recv() != "ready":
            p.kill()
            raise RuntimeError(f"sandbox worker for {self.args[1]} failed to start")
        self._all.append(p)
        return p, parent

    def run(self, code: str, as_of: str, shadow: tuple[str, str] | None = None) -> dict:
        """shadow: (rule-normalized question, period call) for the semantic-layer cross-check."""
        p, conn = self._free.get()
        try:
            conn.send((code, str(as_of), shadow))
            if conn.poll(self.timeout + 30):
                return conn.recv()
            p.kill()
            p, conn = self._start()
            return {"result": None, "error": f"hard timeout after {self.timeout + 30}s", "stdout": ""}
        except (EOFError, BrokenPipeError, OSError) as e:
            p.kill()
            p, conn = self._start()
            return {"result": None, "error": f"sandbox worker crashed: {e!r}", "stdout": ""}
        finally:
            self._free.put((p, conn))

    def close(self) -> None:
        while not self._free.empty():
            p, conn = self._free.get()
            with contextlib.suppress(Exception):
                conn.send(None)
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
