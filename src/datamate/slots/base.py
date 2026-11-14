"""Run a shop's slots: for each convention the loaders may miss, a trigger computed from the data, a prompt (the shop's
README, this split's rulebook rows on the topic, the columns that could hold it), a narrow JSON the model fills, a parse
that keeps only real columns, values and labels, the pure apply of slots/apply.py, and a physical check of the result.

A slot fires on a gap in the data (every order 'hoàn thành' while a text column could say otherwise), not on a rule that
knows it failed: the rules that build the store never know. A fill that does not parse or fails its check is dropped and
the store stays as the rules built it, so a slot can only add. Slots run in order and each sees the ones before it.
Fills are saved next to the ingest cache (`<biz>.slots.json`); a model reply is reused while its prompt is unchanged.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from datamate.audit import _LINE_DISCOUNT
from datamate.canon import build_sales, line_table
from datamate.ingest.business import META_COLS, Business, build_business, slots_path, status_path, apply_status
from datamate.ingest.roles import header_key, is_known_status, normalize_status
from datamate.slots.apply import STATUSES, name_map, order_key_roles
from datamate.text import fold

SLOTS_VERSION = 1
ORDER = ("order_key", "status_text", "name_map", "line_amount")
CANON = {"order_id", "date", "order_ts", "status", "counted", "order_ok", "is_goods", "amount", "qty", "qty_base", "unit",
         "base_unit", "unit_price", "product_id", "product_key", "product_name", "customer", "customer_key", "customer_id",
         "customer_name", "customer_group", "walkin", "product_name_raw", "customer_name_raw"}
_JSON = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


@dataclass
class Ctx:
    biz: Business
    sales: pd.DataFrame | None
    line: str | None  # the table build_sales reads its lines from
    rulebook: str


def _roles(biz: Business) -> dict[str, dict[str, str]]:
    return {n: ti.roles for n, ti in biz.info.items()}


def ctx(biz: Business, rulebook: str, applied: dict) -> Ctx:
    sales = build_sales(biz.tables, _roles(biz), applied)[0]
    return Ctx(biz, sales, line_table(biz.tables, _roles(biz)), rulebook)


def rule_rows(rulebook: str, pattern: str, limit: int = 20) -> str:
    rows = [ln for ln in rulebook.splitlines() if re.search(pattern, ln, re.I)]
    return "\n".join(rows[:limit]) or "(none)"


def value_card(s: pd.Series, name: str, n_all: int, list_max: int = 30) -> str:
    v = s.dropna()
    k = v.nunique()
    if k <= list_max:
        vals = v.value_counts()
        return f'- "{name}" ({len(v)} of {n_all} rows filled): ' + ", ".join(f"{x!r} ({m})" for x, m in vals.items())
    ex = [repr(str(x)[:30]) for x in v.unique()[:4]]
    return f'- "{name}" ({len(v)} of {n_all} rows filled, {k} distinct): e.g. ' + ", ".join(ex)


def parse_json(reply: str):
    text = str(reply or "")
    m = _JSON.search(text)
    body = m.group(1) if m else text[text.find("{"):text.rfind("}") + 1] if "{" in text else text.strip()
    try:
        return json.loads(body)
    except ValueError:
        return None


def _readme(c: Ctx) -> str:
    return "## README of the shop\n" + c.biz.readme.text.strip()


def _same(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and fold(a).strip() == fold(b).strip()


# --- 0. order_key: the column that identifies an order, when no header rule found one -------------------------------


def _day(t: pd.DataFrame, roles: dict[str, str]) -> pd.Series | None:
    col = next((c for c, r in roles.items() if r in ("order_date", "order_ts") and c in t.columns), None)
    return None if col is None else pd.to_datetime(t[col], errors="coerce").dt.normalize()


def key_shape(t: pd.DataFrame, col: str, day: pd.Series | None) -> str | None:
    """Why `col` cannot be an order code (None when it can): filled on nearly every row, shared by the lines of an order
    (not a row number), one day per order."""
    v = t[col]
    if pd.api.types.is_datetime64_any_dtype(v) or v.dropna().map(lambda x: isinstance(x, pd.Timestamp)).any():
        return "a date"
    if v.notna().mean() < 0.95:
        # rulebook §8: the code only on an order's first row, blank on the rows under it (each code written once)
        sparse = v.notna().mean() >= 0.2 and pd.notna(v.iloc[0]) and v.dropna().is_unique
        if not sparse:
            return f"blank on {1 - v.notna().mean():.0%} of rows"
        v, day = v.ffill(), None if day is None else day.ffill()
    k = v.nunique()
    if k < 2 or v.notna().sum() / k < 1.05:
        return "one row per value (a row number, not an order)" if k >= 2 else "one value"
    if day is not None:
        one = (day.groupby(v).nunique() <= 1).mean()
        if one < 0.99:
            return f"only {one:.0%} of its values fall on a single day"
    return None


def _key_candidates(c: Ctx) -> list[str]:
    t, roles = c.biz.tables[c.line], c.biz.info[c.line].roles
    day = _day(t, roles)
    return [col for col in t.columns if col not in META_COLS and not roles.get(col)
            and not str(col).endswith(("__norm", "__unit")) and key_shape(t, col, day) is None]


def ok_trigger(c: Ctx) -> str | None:
    # a line table without its order code may build no sales at all: its date sits on the order header
    if c.line is None or (c.sales is not None and "order_id" in c.sales.columns):
        return None
    cands = _key_candidates(c)
    return f"no order code among the roles of T['{c.line}']; columns shaped like one: {cands}" if cands else None


OK_SYSTEM = """You read one shop's sales table for an analyst. Each row is one line of an order (a bill); the lines of one
order share an order code. The code that loaded the table found no column holding that code. Below: the columns that
could be it with their values, a few rows of the table, and the README.

Fill this JSON and reply with it alone, in a ```json block:
{"order_column": "<the column whose value is the code or number of the order the row belongs to, or null if none is>"}
A running row number, a customer, a phone, a price or an amount is not an order code."""


def ok_prompt(c: Ctx) -> tuple[str, list[str]]:
    t = c.biz.tables[c.line]
    descs = c.biz.info[c.line].descs
    cards = [value_card(t[col], col, len(t)) + (f" (README: {descs[col]})" if col in descs else "") for col in _key_candidates(c)]
    rows = t[[col for col in t.columns if col not in META_COLS]].head(8).to_string(index=False, max_colwidth=24)
    return OK_SYSTEM, [f"## Columns of T['{c.line}'] that could hold the order code\n" + "\n".join(cards),
                       f"## First rows\n{rows}", _readme(c)]


def ok_parse(raw, c: Ctx) -> dict | None:
    col = raw.get("order_column") if isinstance(raw, dict) else None
    return {"table": c.line, "column": col} if col in _key_candidates(c) else None


def ok_check(before: Ctx, after: Ctx, fill: dict) -> str | None:
    if after.sales is None or "order_id" not in after.sales.columns:
        return "the store still has no order code"
    if before.sales is not None and len(after.sales) < 0.95 * len(before.sales):
        return f"{len(before.sales) - len(after.sales)} lines lost"
    return None


# --- 1. status_text: the order status written in a text column (notes, delivery state) -------------------------------


def _text_candidates(c: Ctx) -> list[str]:
    s = c.sales
    out = []
    for col in s.columns:
        if col in CANON or str(col).endswith(" (as written)") or not (s[col].dtype == object or pd.api.types.is_string_dtype(s[col])):
            continue
        v = s[col].dropna()
        if len(v) and v.map(lambda x: isinstance(x, str)).all() and v.nunique() <= 50:
            out.append(col)
    return out


def st_trigger(c: Ctx) -> str | None:
    if c.sales is None or set(c.sales["status"].dropna().unique()) - {"hoàn thành"}:
        return None
    cands = _text_candidates(c)
    return f"every order reads 'hoàn thành'; text columns without a role: {cands}" if cands else None


ST_SYSTEM = f"""You read one shop's sales table for an analyst. The rulebook says every order has exactly one status among
{', '.join(STATUSES)}. The code that loaded the table found no status column, so every order reads 'hoàn thành'.
Below: the shop's text columns without a role with their values, the README, and the rulebook rows on order status.

Fill this JSON and reply with it alone, in a ```json block:
{{"status_column": "<the column whose values state the order status, or null if none does>",
  "status_map": {{"<value exactly as listed>": "<one of {' | '.join(STATUSES)}>"}}}}
Map every listed value that states a status, in any spelling; leave out values that do not."""


def st_prompt(c: Ctx) -> tuple[str, list[str]]:
    s = c.sales
    cards = [value_card(s[col], col, len(s), list_max=50) for col in _text_candidates(c)]
    return ST_SYSTEM, ["## Text columns of the sales table\n" + "\n".join(cards), _readme(c),
                       "## RULEBOOK rows on order status\n" + rule_rows(c.rulebook, r"trạng thái|huỷ|hủy|trả hàng|đang giao")]


def _known_status_column(c: Ctx) -> str | None:
    """The one text column whose every value the rules read as a status (train vocabulary), one of them not finished."""
    hold = [k for k in _text_candidates(c)
            if all(is_known_status(v) for v in c.sales[k].dropna().unique())
            and any(normalize_status(v) != "hoàn thành" for v in c.sales[k].dropna().unique())]
    return hold[0] if len(hold) == 1 else None


def st_parse(raw, c: Ctx) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("status_map"), dict) or not raw.get("status_map"):
        col = _known_status_column(c)  # the model named nothing: a column the train vocabulary reads in full
        raw = {"status_column": col, "status_map": {}} if col else None
    if raw is None:
        return None
    col = raw.get("status_column")
    if col not in _text_candidates(c):  # no column named, or a wrong one: the column whose values the map lists, if only one
        keys = {fold(k).strip() for k in raw["status_map"] if isinstance(k, str)}
        hold = [k for k in _text_candidates(c) if keys and keys <= set(c.sales[k].dropna().map(lambda v: fold(v).strip()))]
        if len(hold) != 1:
            return None
        col = hold[0]
    values = {fold(v).strip(): v for v in c.sales[col].dropna().unique()}
    smap = {values[fold(k).strip()]: lab for k, lab in (raw.get("status_map") or {}).items()
            if isinstance(k, str) and fold(k).strip() in values and lab in STATUSES}
    for v in values.values():  # a value the model left out that the rules read as a status (train vocabulary)
        if v not in smap and isinstance(v, str) and is_known_status(v):
            smap[v] = normalize_status(v)
    return {"column": col, "map": smap} if any(lab != "hoàn thành" for lab in smap.values()) else None


def st_check(before: Ctx, after: Ctx, fill: dict) -> str | None:
    s = after.sales
    if s is None or len(s) != len(before.sales):
        return "line count changed"
    bad = s["status"].isin(["huỷ", "trả hàng"])
    share = bad.groupby(s["order_id"]).max().mean() if "order_id" in s.columns else bad.mean()
    return None if 0 < share <= 0.30 else f"{share:.1%} of orders cancelled or returned"


# --- 2. name_map: product names written short, with the shop's list of short forms ----------------------------------


def _lists(c: Ctx) -> set[str]:
    """Folded names of the product lists (catalogues, stock): not the line table, not purchases."""
    known: set[str] = set()
    for n, ti in c.biz.info.items():
        col = next((k for k, r in ti.roles.items() if r == "product_name"), None)
        if n != c.line and col and not re.search(r"nhap|import|ncc", fold(n)):
            known |= set(c.biz.tables[n][col].dropna().map(lambda v: fold(v).strip()))
    return known


def _gap(c: Ctx) -> tuple[float, pd.Series]:
    s = c.sales
    names = s.loc[s["is_goods"] & s["product_name"].notna(), "product_name"].map(lambda v: fold(v).strip())
    miss = names[~names.isin(_lists(c))]
    return (len(miss) / len(names) if len(names) else 0.0), miss


def _map_tables(c: Ctx, miss: pd.Series) -> list[str]:
    """Small tables with a text column that holds most of the names no product list knows."""
    out = []
    for n, t in c.biz.tables.items():
        cols = [k for k in t.columns if k not in META_COLS and not str(k).endswith("__norm")]
        text = [k for k in cols if t[k].map(lambda v: isinstance(v, str)).mean() > 0.9]
        if n == c.line or len(cols) > 8 or len(text) < 2:
            continue
        # one row per short form, next to a longer name (a unit list repeats its product and has no longer column)
        uniq = [k for k in text if t[k].nunique() >= 0.9 * t[k].notna().sum()]
        size = {k: t[k].dropna().astype(str).str.len().mean() for k in uniq}
        if any(miss.isin(set(t[k].dropna().map(lambda v: fold(v).strip()))).mean() >= 0.5
               and any(size[j] > size[k] for j in uniq if j != k) for k in uniq):
            out.append(n)
    return out


def nm_trigger(c: Ctx) -> str | None:
    if c.sales is None or "product_name" not in c.sales.columns:
        return None
    gap, miss = _gap(c)
    if gap < 0.3:
        return None
    tabs = _map_tables(c, miss)
    return f"{gap:.0%} of goods lines name a product no list has; tables holding those names: {tabs}" if tabs else None


NM_SYSTEM = """You read one shop's records for an analyst. Most sales lines name the product in a short form that no
product list has. Below: the short names most sold, the small tables of the shop that hold them, and the README.
A table with one row per short form and, next to it, the product's full name is the list of short forms: name it, its
column of short forms and its column of full names.

Fill this JSON with table and column NAMES (headers, as listed), not with product names, and reply with it alone, in a
```json block:
{"table": "<name of the table that lists the short forms, or null if none does>",
 "short_col": "<header of its column of short forms>", "full_col": "<header of its column of full product names>"}"""


def nm_prompt(c: Ctx) -> tuple[str, list[str]]:
    _, miss = _gap(c)
    s = c.sales
    shown = s.loc[s["product_name"].notna(), "product_name"]
    shown = shown[shown.map(lambda v: fold(v).strip()).isin(set(miss))]
    top = ", ".join(f"{k!r} ({m})" for k, m in shown.value_counts().head(15).items())
    tabs = []
    for n in _map_tables(c, miss):
        t = c.biz.tables[n]
        cols = [k for k in t.columns if k not in META_COLS]
        tabs.append(f"table {n!r} ({len(t)} rows), column headers {cols}:\n" + t[cols].head(8).to_string(index=False, max_colwidth=32))
    return NM_SYSTEM, [f"## Short names on the sales lines (with line counts)\n{top}",
                       "## Small tables\n" + "\n\n".join(tabs), _readme(c)]


def nm_parse(raw, c: Ctx) -> dict | None:
    if not isinstance(raw, dict):
        return None
    _, miss = _gap(c)
    short, full = raw.get("short_col"), raw.get("full_col")
    t = raw.get("table")
    if t not in _map_tables(c, miss):  # no table named, or a wrong one: the one table that has both columns
        hold = [n for n in _map_tables(c, miss) if {short, full} <= set(c.biz.tables[n].columns)]
        if len(hold) != 1:
            return None
        t = hold[0]
    cols = c.biz.tables[t].columns
    if short not in cols or full not in cols or short == full or not name_map(c.biz.tables[t], short, full):
        return None
    col = c.biz.info[c.line].roles
    pcol = next((k for k, r in col.items() if r == "product_name"), None)
    return {"line_table": c.line, "column": pcol, "table": t, "short_col": short, "full_col": full} if pcol else None


def nm_check(before: Ctx, after: Ctx, fill: dict) -> str | None:
    if after.sales is None or len(after.sales) != len(before.sales):
        return "line count changed"
    g0, g1 = _gap(before)[0], _gap(after)[0]
    return None if g1 <= g0 / 2 else f"names no list has: {g0:.0%} before, {g1:.0%} after"


# --- 3. line_amount: amounts written before a line discount ---------------------------------------------------------


def _discount_cols(c: Ctx) -> list[str]:
    s = c.sales
    if s is None or not {"order_id", "qty", "unit_price", "amount"} <= set(s.columns):
        return []
    descs = c.biz.info[c.line].descs if c.line in c.biz.info else {}
    q, p, a = (pd.to_numeric(s[k], errors="coerce") for k in ("qty", "unit_price", "amount"))
    out = []
    for col in s.columns:
        if col in CANON or not pd.api.types.is_numeric_dtype(s[col]):
            continue
        said = re.search(r"giam|chiet khau|khuyen mai", fold(descs.get(col, "")))
        if not (_LINE_DISCOUNT.match(header_key(col)) or said):
            continue
        d = pd.to_numeric(s[col], errors="coerce")
        multi = d.groupby(s["order_id"]).agg(["size", "nunique", "max"])
        multi = multi[(multi["size"] > 1) & (multi["max"] > 0)]  # orders with a discount: all-zero orders say nothing
        if len(multi) >= 30 and (multi["nunique"] <= 1).mean() >= 0.95:
            continue  # one value on every line of an order: the order's discount, which revenue keeps (rulebook)
        rows = (d > 0) & q.notna() & p.notna() & a.notna() & (a != 0)
        if rows.sum() >= 30 and ((q * p - a).abs() <= 1)[rows].mean() >= 0.95:
            out.append(col)
    return out


def la_trigger(c: Ctx) -> str | None:
    cols = _discount_cols(c)
    return f"line discount columns {cols} > 0 while amount = qty × unit_price on those lines" if cols else None


LA_SYSTEM = """You read one shop's sales table for an analyst. On the lines below, a discount column is above 0 while the
line amount still equals quantity × unit price. The rulebook rows on discounts and the README say what the column is.

Fill this JSON and reply with it alone, in a ```json block:
{"discount_column": "<the column>", "kind": "<amount (money off the line) | percent (percent off the line)>",
 "level": "<line (a discount of this line, not yet taken off its amount) | order (a discount of the whole order, or already in the amount)>"}"""


def la_prompt(c: Ctx) -> tuple[str, list[str]]:
    s = c.sales
    cols = _discount_cols(c)
    d = pd.to_numeric(s[cols[0]], errors="coerce")
    rows = s.loc[d > 0, ["order_id", "qty", "unit_price", "amount", *cols]].head(10).to_string(index=False)
    descs = c.biz.info[c.line].descs
    cards = [value_card(s[k], k, len(s)) + (f" (README: {descs[k]})" if k in descs else "") for k in cols]
    return LA_SYSTEM, ["## Discount columns\n" + "\n".join(cards), f"## Lines with a discount\n{rows}", _readme(c),
                       "## RULEBOOK rows on discounts\n" + rule_rows(c.rulebook, r"giảm|chiết khấu|khuyến mãi|thành tiền")]


def la_parse(raw, c: Ctx) -> dict | None:
    if not isinstance(raw, dict) or raw.get("discount_column") not in _discount_cols(c):
        return None
    if raw.get("kind") not in ("amount", "percent") or raw.get("level") != "line":
        return None  # an order discount stays in revenue: nothing to apply
    return {"column": raw["discount_column"], "kind": raw["kind"]}


def la_check(before: Ctx, after: Ctx, fill: dict) -> str | None:
    s0, s1 = before.sales, after.sales
    if s1 is None or len(s1) != len(s0):
        return "line count changed"
    d = pd.to_numeric(s1[fill["column"]], errors="coerce") > 0
    gross = pd.to_numeric(s1["qty"], errors="coerce") * pd.to_numeric(s1["unit_price"], errors="coerce")
    ok = ((s1["amount"] <= gross + 1) & (s1["amount"] >= -1))[d].mean()
    r0, r1 = s0.loc[s0["counted"], "amount"].sum(), s1.loc[s1["counted"], "amount"].sum()
    if ok < 0.95:
        return f"amount within 0..qty × price on only {ok:.0%} of discounted lines"
    return None if r0 and abs(r1 - r0) / abs(r0) < 0.2 else f"revenue moved {r0:,.0f} -> {r1:,.0f}"


@dataclass(frozen=True)
class Slot:
    name: str
    trigger: Callable[[Ctx], str | None]
    prompt: Callable[[Ctx], tuple[str, list[str]]]
    parse: Callable[[object, Ctx], dict | None]
    check: Callable[[Ctx, Ctx, dict], str | None]


SLOTS = {s.name: s for s in (Slot("order_key", ok_trigger, ok_prompt, ok_parse, ok_check),
                             Slot("status_text", st_trigger, st_prompt, st_parse, st_check),
                             Slot("name_map", nm_trigger, nm_prompt, nm_parse, nm_check),
                             Slot("line_amount", la_trigger, la_prompt, la_parse, la_check))}


def run_slots(biz: Business, rulebook: str, generate: Callable[[str, list[str]], str] | None,
              rebuild: Callable[[dict], Business], enabled=ORDER, replies: dict | None = None) -> tuple[dict, dict]:
    """(applied fills by slot, record by slot). generate None: triggers only (dry run). rebuild(extra_roles) loads the
    shop again with more column roles (order_key). replies: slot -> (prompt sha, reply) saved by an earlier run."""
    applied, record = {}, {}
    cur = ctx(biz, rulebook, applied)
    for name in ORDER:
        if name not in enabled:
            continue
        slot = SLOTS[name]
        why = slot.trigger(cur)
        rec = record[name] = {"trigger": why}
        if why is None:
            rec["result"] = "no gap"
            continue
        if generate is None:
            rec["result"] = "dry run"
            continue
        system, parts = slot.prompt(cur)
        sha = hashlib.sha1("\n".join([system, *parts]).encode()).hexdigest()
        old = (replies or {}).get(name)
        reply = old[1] if old and old[0] == sha else generate(system, parts)
        rec.update(prompt_sha=sha, reply=str(reply)[:3000])
        fill = slot.parse(parse_json(reply), cur)
        if fill is None:
            rec["result"] = "no fill"
            continue
        rec["fill"] = fill
        nxt_biz = rebuild({**cur.biz.extra_roles, **{t: {**cur.biz.extra_roles.get(t, {}), **r}
                                                     for t, r in order_key_roles({name: fill}).items()}}) if name == "order_key" else cur.biz
        trial = {**applied, name: fill}
        nxt = ctx(nxt_biz, rulebook, {k: v for k, v in trial.items() if k != "order_key"})
        bad = slot.check(cur, nxt, fill)
        if bad:
            rec["result"] = f"rejected: {bad}"
            continue
        rec["result"] = "applied"
        applied, cur = trial, nxt
    return applied, record


def disk_rebuild(split_dir: Path, biz_id: str, cache_dir: Path | None) -> Callable[[dict], Business]:
    def rebuild(extra: dict) -> Business:
        biz = build_business(split_dir, biz_id, extra)
        labels = status_path(cache_dir, Path(split_dir).name, biz_id) if cache_dir else None
        if labels and labels.exists():
            apply_status(biz, json.loads(labels.read_text(encoding="utf-8")))
        return biz
    return rebuild


def slots_key(biz: Business, rulebook: str, model: str, enabled) -> str:
    prompts = [OK_SYSTEM, ST_SYSTEM, NM_SYSTEM, LA_SYSTEM]  # a changed prompt asks again
    return hashlib.sha1(json.dumps([SLOTS_VERSION, biz.readme.text, rulebook, model, sorted(enabled), prompts]).encode()).hexdigest()


def resolve_slots(biz: Business, split_dir: Path, cache_dir: Path, rulebook: str, generate, model: str,
                  enabled=ORDER) -> bool:
    """Fill this shop's slots once per (README, rulebook, model, slots on). True when the saved fills changed: load the
    shop again to get them."""
    path = slots_path(cache_dir, biz.split, biz.id)
    key = slots_key(biz, rulebook, model, enabled)
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if old.get("key") == key:
        return False
    replies = {n: (r["prompt_sha"], r["reply"]) for n, r in (old.get("record") or {}).items() if r.get("prompt_sha")} \
        if old.get("model") == model else {}
    base = biz
    saved_key = old.get("applied", {}).get("order_key")
    if saved_key:  # the saved order code is in biz's roles: start from the shop the rules and roles.json give
        extra = {t: {c: r for c, r in cols.items() if not (t == saved_key["table"] and c == saved_key["column"])}
                 for t, cols in biz.extra_roles.items()}
        base = disk_rebuild(split_dir, biz.id, cache_dir)({t: cols for t, cols in extra.items() if cols})
    applied, record = run_slots(base, rulebook, generate, disk_rebuild(split_dir, biz.id, cache_dir), enabled, replies)
    path.write_text(json.dumps({"key": key, "model": model, "applied": applied, "record": record}, ensure_ascii=False,
                               indent=1, default=str), encoding="utf-8")
    return applied != old.get("applied", {})
