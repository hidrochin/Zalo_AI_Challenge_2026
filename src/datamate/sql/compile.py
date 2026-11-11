"""Rulebook compiler: the model reads the split's rulebook once and writes the DuckDB macros its definitions need.

Nothing here knows a rule. The model gets the rulebook section by section, the canonical schema every store shares and
the macros it already wrote, and returns one `create macro` per definition a question can ask for. Each macro is
created and called on the split's stores; one that fails everywhere goes back to the model once with the error.
At answer time every shop gets the macros that run on its store (`usable`), as temp macros that shadow stored ones.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import duckdb

COMPILE_VERSION = 1

# What every store holds when the shop has the data (sql/store.py, facts.py). Facts only, no rule: the compiler decides
# how the rulebook combines them.
SCHEMA_DOC = """## Canonical tables (a shop has the ones its files support; quote every column name: "date")
- `sales_lines` (view, rows up to today) / `sales_all` (every row, also after today): one row per sales line.
  order_id, "date" (order date), order_ts (order timestamp, some shops), status (one of 'hoàn thành', 'huỷ', 'trả hàng',
  'đang giao'; the status of the whole order), counted (goods line of an order that is neither cancelled nor returned and
  has no returned line), order_ok (the line's order is neither cancelled nor returned), is_goods (false for a line without
  a quantity: a discount or fee line), amount (line amount after line discount, VND; negative for a returned line), qty,
  unit (unit sold), qty_base (quantity in the product's base unit), base_unit, unit_price, product_key, product_id,
  product_name (one spelling per product), customer (customer key: code if the shop has codes, else name; NULL for
  walk-in customers), customer_key, customer_id, customer_name, customer_group, walkin (true for a walk-in customer).
  Other columns keep the shop's own names (channel, payment method, staff, a cost per unit sold...).
- `orders` (view) / `orders_all`: one row per order: order_id, "date", order_ts, status, counted, customer, customer_name,
  customer_group, walkin, goods_amount (sum of the order's line amounts), n_lines, plus order-level columns of the shop.
- `debt_orders_all`: one row per order that carries a total: order_id, "date", status, customer, customer_name, walkin,
  total (what the customer owes for the whole order: after order discount, with shipping), on_credit (the order was
  recorded as sold on credit), can_owe (payment method is not COD and the channel is not a marketplace).
- `payments` (view) / `payments_all`: order_id, "date", amount: one row per payment received for an order.
- `deliveries` (view) / `deliveries_all`: order_id, trip, "date": one row per delivery trip of an order.
- `imports` (view) / `imports_all`: doc (receipt), "date", supplier, product_name, qty, unit, price, amount, qty_base;
  cancelled receipts are already dropped.
- `expenses_all`: "date", amount: one row per expense entry.
- `stock_card_all`: product_name, ts, seq, balance (stock in base units after each move).
- `stock_report_all`: product_name, qty (stock in base units at the end of the data).
- `lots_all`: lot, product_name, expiry, qty (base units received), price (per base unit).
  `lot_sales_all`: lot, order_id, qty, returned (base units sold from the lot by an order).
- `product_units`: product_name, unit, factor (base units in one `unit`).
- `catalog_cost_all`: product_name, cost (cost of one base unit in the product list).
- `price_list_all`: product_name, "date", price (a selling price per base unit from that day).
- `calendar`: "date", weekday (1 = Monday .. 7 = Sunday), lunar_day, lunar_month, lunar_year, lunar_leap, holiday.

## Helpers already defined
as_of() = today; round_half_up(x, n); pct_change(new, old) (NULL when old is 0); has_name(col, 'text') (contains,
ignoring case and diacritics); same_text(col, 'text') (equal, ignoring case, diacritics, punctuation); fold_text(x);
flag(col) (a yes/no cell as boolean); shift_of(ts) ('sáng' / 'chiều' by the rulebook's shift hours, else NULL);
stock_at(product_name, d) (stock at the end of day d, base units); lots_left(d) (TABLE lot, product_name, expiry, left,
price: lots with stock at the end of day d)."""

SYSTEM = """You turn the rulebook of a data-analysis contest into DuckDB SQL macros. Analysts call your macros to answer
everyday questions about a small shop's data, so each macro must apply its rule exactly as written.

{schema}

## How to write a macro
- One ```sql block per macro. Its first lines are comments, then one statement:
  -- doc: what it returns, in the rulebook's words, and the Vietnamese phrases a question uses for it
  -- args: a date, b date   (each argument with its type: date, int, number, text)
  create macro name(a, b) as (select ...)            -- one value
  create macro name(a, b) as table select ...        -- rows: a ranking, a list, a per-object table
- Names are short English snake_case. Arguments: a, b = first and last day of the period (both included); d = a day.
- Use only the tables, columns and helpers listed above, and the macros already written (listed in the message).
- Today is as_of(). The views stop at today; when you read an `*_all` table, keep rows dated <= least(<day>, as_of()).
- Money is VND rounded to đồng; percents are 0..100 rounded to 2 decimals unless the rule says otherwise.
- Rankings: descending by the criterion, ties by the key ascending.
- Write a macro only for a rule that defines a quantity, a set or a state a question can ask for. Skip rules about
  wording, time expressions, answer format and scoring. If a section has none, reply with the one word NONE.
- A rule may refer to another section (§1, §3): apply that section too, or call its macro.

## The whole RULEBOOK (you write the macros of one section per message)
{rulebook}
"""

_BLOCK = re.compile(r"```(?:sql|duckdb)?\s*\n(.*?)```", re.S | re.I)
_HEAD = re.compile(r"create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?macro\s+(\w+)\s*\(([^)]*)\)\s+as\s+(table\b)?", re.I)
_DOC = re.compile(r"^\s*--\s*doc\s*:\s*(.+)$", re.M | re.I)
_ARGS = re.compile(r"^\s*--\s*args\s*:\s*(.+)$", re.M | re.I)
_SECTION = re.compile(r"^##\s+", re.M)


@dataclass
class Macro:
    name: str
    args: list[str]
    types: dict[str, str]
    table: bool
    doc: str
    sql: str  # the create statement, as the model wrote it
    section: str = ""
    shops: list[str] = field(default_factory=list)  # stores it ran on during the compile check

    @property
    def signature(self) -> str:
        return f"{self.name}({', '.join(self.args)})"

    def card_line(self) -> str:
        return f"{self.signature}{': TABLE' if self.table else ''}: {self.doc}"

    def temp_sql(self) -> str:
        return _HEAD.sub(lambda m: f"create or replace temp macro {m.group(1)}({m.group(2)}) as {m.group(3) or ''}", self.sql, count=1)


def sections(rulebook: str) -> list[tuple[str, str]]:
    """(title, text) of each '## ' section; the text before the first section is the preamble every call gets."""
    parts = _SECTION.split(rulebook)
    return [(p.splitlines()[0].strip(), "## " + p.strip()) for p in parts[1:] if p.strip()]


def parse_macros(reply: str, section: str = "") -> list[Macro]:
    """Every `create macro` of the reply; a block may hold several, each after its own `-- doc:` / `-- args:` lines."""
    out = []
    for block in _BLOCK.findall(reply):
        heads = list(_HEAD.finditer(block))
        ends = [m.start() for m in heads[1:]] + [len(block)]
        prev = 0
        for m, end in zip(heads, ends):
            lines = block[m.start():end].rstrip().splitlines()
            while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("--")):  # the next macro's comments
                lines.pop()
            body = "\n".join(lines)
            head, prev = block[prev:m.start()], m.start() + len(body)
            args = [a.strip() for a in m.group(2).split(",") if a.strip()]
            types = {}
            t = _ARGS.search(head)
            if t:
                for part in t.group(1).split(","):
                    words = part.split()
                    if len(words) >= 2:
                        types[words[0]] = words[1].lower()
            doc = _DOC.search(head)
            out.append(Macro(m.group(1).lower(), args, types, bool(m.group(3)), doc.group(1).strip() if doc else "",
                             body.strip().rstrip(";").strip(), section))
    return out


def _probe_args(macro: Macro, day: date) -> str:
    vals = []
    for a in macro.args:
        t = macro.types.get(a, "")
        low = a.lower()
        if t == "date" or (not t and (low in ("a", "b", "d", "x") or "date" in low or "day" in low)):
            first = low in ("a", "start", "from_date") or low.startswith(("start", "from"))
            vals.append(f"date '{(day - timedelta(days=29)) if first else day}'")
        elif t in ("int", "integer", "number") or low in ("n", "k"):
            vals.append("3")
        else:
            vals.append("'x'")
    return ", ".join(vals)


_CALL = re.compile(r"\b(\w+)\s*\(")


def try_macro(con, macro: Macro, day: date, hidden: frozenset[str] = frozenset()) -> str | None:
    """Create the macro on this cursor and call it once; the error text, or None when it ran.
    hidden: stored macros the run does not offer (hand-written rule macros): calling one is an error, as for a query."""
    body = macro.sql[_HEAD.search(macro.sql).end():] if _HEAD.search(macro.sql) else macro.sql
    called = next((n for n in _CALL.findall(body) if n.lower() in hidden), None)
    if called:
        return f"Catalog Error: Function with name {called} does not exist!"
    try:
        con.execute(macro.temp_sql())
        call = f"{macro.name}({_probe_args(macro, day)})"
        con.execute(f"select * from {call} limit 1" if macro.table else f"select {call}").fetchall()
        return None
    except duckdb.Error as e:
        try:
            con.execute(f"drop macro {'table ' if macro.table else ''}if exists temp.{macro.name}")
        except duckdb.Error:
            pass
        return str(e).splitlines()[0][:400]


def data_end(con) -> date:
    try:
        hi = con.execute('select max("date") from sales_all').fetchone()[0]
    except duckdb.Error:
        hi = None
    return hi or date(2026, 1, 1)


def _hidden(hidden: frozenset[str], macros: list[Macro]) -> frozenset[str]:
    return hidden - {m.name for m in macros}


def usable(con, macros: list[Macro], hidden: frozenset[str] = frozenset()) -> list[Macro]:
    """The macros that run on this shop's store, created on `con` in order (a later macro may call an earlier one)."""
    day = data_end(con)
    con.execute(f"set variable as_of = date '{day}'")
    hide = _hidden(hidden, macros)
    return [m for m in macros if try_macro(con, m, day, hide) is None]


def _missing_table(err: str) -> bool:
    return bool(re.search(r"Table with name \w+ does not exist|Referenced table|Catalog Error: Table", err))


def _check(macros: list[Macro], stores: dict[str, Path], before: list[Macro], hidden: frozenset[str]) -> dict[str, str]:
    """Run every macro on every store, after the macros of earlier sections: name -> an error, for the ones that ran
    nowhere (the error of a shop that has the tables, if any). Fills Macro.shops."""
    hide = _hidden(hidden, before + macros)
    errors: dict[str, list[str]] = {m.name: [] for m in macros}
    for m in macros:
        m.shops = []
    for biz, path in stores.items():
        con = duckdb.connect(str(path), read_only=True)
        try:
            usable(con, before, hidden)
            day = data_end(con)
            for m in macros:
                err = try_macro(con, m, day, hide)
                if err is None:
                    m.shops.append(biz)
                else:
                    errors[m.name].append(err)
        finally:
            con.close()
    out = {}
    for m in macros:
        if not m.shops:
            errs = errors[m.name]
            out[m.name] = next((e for e in errs if not _missing_table(e)), errs[0] if errs else "did not run")
    return out


def _known(macros: list[Macro]) -> str:
    return "\n".join(f"- {m.card_line()}" for m in macros) or "(none yet)"


def compile_rulebook(rulebook: str, stores: dict[str, Path], generate: Callable[[str, list[str], list[dict]], str],
                     cache_path: Path, model: str = "", hidden: frozenset[str] = frozenset(),
                     log: Callable[[str], None] = print) -> list[Macro]:
    """The macros of this rulebook, cached by rulebook, schema, version and model.
    generate(system, user_parts, history) -> reply (greedy, thinking as the run sets it).
    hidden: the stores' hand-written rule macros, which the compiled ones may not call."""
    key = hashlib.sha1(f"{COMPILE_VERSION}\n{model}\n{SCHEMA_DOC}\n{SYSTEM}\n{rulebook}".encode()).hexdigest()[:12]
    path = cache_path.with_name(f"{cache_path.stem}_{key}.json")
    if path.exists():
        return [Macro(**d) for d in json.loads(path.read_text(encoding="utf-8"))["macros"]]
    system = SYSTEM.format(schema=SCHEMA_DOC, rulebook=rulebook.strip())
    macros: list[Macro] = []
    record = []
    for title, text in sections(rulebook):
        user = [f"Macros already written:\n{_known(macros)}", f"Write the macros of this section:\n\n{text}"]
        reply = generate(system, user, [])
        new = [m for m in parse_macros(reply, title) if m.name not in {x.name for x in macros}]
        errors = _check(new, stores, macros, hidden) if new else {}
        if errors:  # one repair round with the errors of the macros that ran nowhere
            msg = "These macros failed when called on the shops' databases:\n" + "\n".join(
                f"- {n}: {e}" for n, e in errors.items()) + "\n\nReturn every failed macro again, fixed, one ```sql block each."
            fixed = {m.name: m for m in parse_macros(generate(system, user, [{"role": "assistant", "content": reply},
                                                                                {"role": "user", "content": msg}]), title)}
            new = [fixed.get(m.name, m) if m.name in errors else m for m in new]
            errors = _check(new, stores, macros, hidden)
        kept = [m for m in new if m.name not in errors]
        macros += kept
        record.append({"section": title, "reply": reply, "kept": [m.name for m in kept], "failed": errors})
        log(f"compile: {title}: {len(kept)} macros{f', {len(errors)} failed' if errors else ''}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"macros": [asdict(m) for m in macros], "sections": record}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return macros
