"""Shop views: once per shop, the model reads the README and the raw tables and writes the views the store lacks.

The canonical views (sales_lines, orders, the fact tables) come from the ingest code. A shop can record more than they
hold: a kind of record of its own, a value packed into a text cell, a table the README explains. The model writes a
`create view` over the raw tables for each; every view must run and return rows, one that fails goes back once with the
error. The views are temp views on each cursor (the store is read-only) and the schema card lists them.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import duckdb

SHOPVIEWS_VERSION = 2
MAX_VIEWS = 6

SYSTEM = """You prepare one shop's database so that analysts can answer everyday questions with one SQL query.
The schema card below lists what the database already has: the canonical views (sales_lines, orders and the fact
views) and every raw table copied from the shop's files (raw_*), with sample values. The README explains the files.

Find what the README or the raw tables record that the canonical views do NOT hold in a usable shape, for example:
- a kind of record with its own table that no canonical view covers;
- a value packed into a text cell (a code, a number with its unit, two fields in one cell) that questions would filter on;
- a table whose rows need a join or a cleanup (the README's notes on how to read it) before they can be counted.
For each, write one DuckDB view over the raw tables. Do not rebuild sales_lines, orders or the fact views.

Format: one ```sql block per view, at most {n} views:
-- doc: one line: what one row is, what each column means, and which questions it serves
create view v_name as select ...
- Names start with v_, short English snake_case. Quote column names with double quotes ("Ngày", "Thành tiền").
- Keep every row: no filter on today. Turn dates into DATE and amounts into numbers with try_cast / strptime / regexp.
- If the canonical views already hold everything, reply with the one word NONE."""

_BLOCK = re.compile(r"```(?:sql|duckdb)?\s*\n(.*?)```", re.S | re.I)
_HEAD = re.compile(r"create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?view\s+(\w+)\s+as\s+", re.I)
_DOC = re.compile(r"^\s*--\s*doc\s*:\s*(.+)$", re.M | re.I)


@dataclass
class ShopView:
    name: str
    doc: str
    sql: str
    columns: list[str]
    rows: int

    def temp_sql(self) -> str:
        return _HEAD.sub(lambda m: f"create or replace temp view {m.group(1)} as ", self.sql, count=1)


def parse_views(reply: str) -> list[tuple[str, str, str]]:
    """(name, doc, create statement) of every view of the reply; a block may hold several."""
    out = []
    for block in _BLOCK.findall(reply):
        heads = list(_HEAD.finditer(block))
        ends = [m.start() for m in heads[1:]] + [len(block)]
        prev = 0
        for m, end in zip(heads, ends):
            lines = block[m.start():end].rstrip().splitlines()
            while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("--")):  # the next view's comments
                lines.pop()
            body = "\n".join(lines)
            head, prev = block[prev:m.start()], m.start() + len(body)
            if m.group(1).lower().startswith("v_"):
                doc = _DOC.search(head)
                out.append((m.group(1).lower(), doc.group(1).strip() if doc else "", body.strip().rstrip(";").strip()))
    return out[:MAX_VIEWS]


def try_view(con, name: str, doc: str, sql: str) -> ShopView | str:
    """Create the view on this cursor; the view with its columns, or the error text."""
    view = ShopView(name, doc, sql, [], 0)
    try:
        con.execute(view.temp_sql())
        rel = con.execute(f"select * from {name} limit 0")
        view.columns = [d[0] for d in rel.description]
        view.rows = con.execute(f"select count(*) from {name}").fetchone()[0]
    except duckdb.Error as e:
        try:
            con.execute(f"drop view if exists temp.{name}")
        except duckdb.Error:
            pass
        return str(e).splitlines()[0][:400]
    if view.rows == 0:
        con.execute(f"drop view if exists temp.{name}")
        return "the view returns no rows"
    same = _copy_of(con, view)
    if same:
        con.execute(f"drop view if exists temp.{name}")
        return f"the view repeats the canonical table {same}"
    return view


def _copy_of(con, view: ShopView) -> str | None:
    """The canonical table this view repeats: the same number of rows and half of its columns there by name."""
    try:
        tables = [r[0] for r in con.execute("select table_name from duckdb_tables() where not temporary").fetchall()]
    except duckdb.Error:
        return None
    cols = {c.lower() for c in view.columns}
    for t in tables:
        if t.startswith(("raw_", "dim_")) or t == "calendar":
            continue
        try:
            have = {r[0].lower() for r in con.execute(f"select column_name from duckdb_columns() where table_name = '{t}'").fetchall()}
            if 2 * len(cols & have) >= len(cols) and con.execute(f'select count(*) from "{t}"').fetchone()[0] == view.rows:
                return t
        except duckdb.Error:
            continue
    return None


def shop_views(biz_id: str, card: str, readme: str, con, generate: Callable[[str, list[str], list[dict]], str],
               cache_dir: Path, model: str = "") -> list[ShopView]:
    """The views of one shop, cached by its card, README, version and model. con: a cursor on the shop's store, where
    the views are tried (and left created)."""
    key = hashlib.sha1(f"{SHOPVIEWS_VERSION}\n{model}\n{SYSTEM}\n{readme}\n{card}".encode()).hexdigest()[:12]
    path = cache_dir / f"{biz_id}_{key}.json"
    if path.exists():
        return [ShopView(**d) for d in json.loads(path.read_text(encoding="utf-8"))["views"]]
    system = SYSTEM.format(n=MAX_VIEWS)
    user = [f"README of the shop:\n{readme.strip()}", card]
    reply = generate(system, user, [])
    got = [(n, d, s, try_view(con, n, d, s)) for n, d, s in parse_views(reply)]
    failed = {n: r for n, _, _, r in got if isinstance(r, str)}
    fixed_reply = ""
    if failed:  # one repair round
        msg = ("These views failed:\n" + "\n".join(f"- {n}: {e}" for n, e in failed.items())
               + "\n\nReturn every failed view again, fixed, one ```sql block each (or leave a view out).")
        fixed_reply = generate(system, user, [{"role": "assistant", "content": reply}, {"role": "user", "content": msg}])
        redo = {n: (d, s) for n, d, s in parse_views(fixed_reply)}
        again = []
        for n, d, s, r in got:
            if n in failed and n in redo:
                d, s = redo[n]
                r = try_view(con, n, d, s)
            again.append((n, d, s, r))
        got = again
    views = [r for *_, r in got if not isinstance(r, str)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"views": [asdict(v) for v in views], "reply": reply, "repair": fixed_reply,
                                "failed": {n: r for n, *_, r in got if isinstance(r, str)}}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return views


def card_part(con, views: list[ShopView]) -> str:
    """The card's description of the shop views: doc, columns and two sample rows each."""
    if not views:
        return ""
    out = ["\n## Views written for this shop (from its README and raw tables; check them against the raw tables if unsure)"]
    for v in views:
        out.append(f"\n## View {v.name}  rows={v.rows}\n  {v.doc}\n  columns: " + ", ".join(f'"{c}"' for c in v.columns))
        try:
            sample = con.execute(f"select * from {v.name} limit 2").fetchall()
            out += [f"  e.g. {tuple(r)!r}"[:400] for r in sample]
        except duckdb.Error:
            pass
    return "\n".join(out)
