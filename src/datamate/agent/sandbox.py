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


def _execute(code: str, as_of: str, tables, cal, timeout: int, sales=None) -> dict:
    import numpy as np
    import pandas as pd

    from datamate import dm

    dm.set_context(as_of, cal, sales)
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
        result = {"type": "unanswerable", "reason": str(e)}
    except BaseException:
        error = _short_tb()
    finally:
        signal.alarm(0)
    if result is not None:
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as e:
            result, error = None, f"answer is not JSON-serializable: {e}"
    return {"result": result if error is None else None, "error": error, "stdout": buf.getvalue()[-3000:]}


def _worker(conn, split_dir: str, biz_id: str, cache_dir: str | None, timeout: int) -> None:
    from datamate.canon import build_sales
    from datamate.ingest import load_business, load_calendar

    biz = load_business(Path(split_dir), biz_id, Path(cache_dir) if cache_dir else None)
    cal = load_calendar(Path(split_dir))
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()})

    def on_alarm(signum, frame):
        raise TimeoutError(f"program exceeded {timeout}s")

    signal.signal(signal.SIGALRM, on_alarm)
    conn.send("ready")
    while True:
        msg = conn.recv()
        if msg is None:
            break
        code, as_of = msg
        conn.send(_execute(code, as_of, biz.tables, cal, timeout, sales))


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

    def run(self, code: str, as_of: str) -> dict:
        p, conn = self._free.get()
        try:
            conn.send((code, str(as_of)))
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
