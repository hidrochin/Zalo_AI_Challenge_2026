"""Apply the slots a shop's model filled (base.py), deterministically. Pure functions: tables and lines in, tables and
lines out; nothing here calls a model or reads a file.

- `order_key` names the column that identifies an order: it becomes the `order_id` role at ingest
  (business.load_business), so the rulebook's §8 fills and duplicate rows run on it as on any order code.
- `name_map` rewrites the line table's short product names to the full names of the shop's abbreviation list, before
  canon.build_sales joins the product lists.
- `status_text` reads the order status from a text column (notes, delivery state) and `line_amount` takes a line discount
  off amounts written before it; both change the lines before `order_ok` and `counted` are derived.
"""

import pandas as pd

from datamate.text import fold

RANK = {"huỷ": 3, "trả hàng": 2, "đang giao": 1}  # one status per order: the worst one written on any of its lines
STATUSES = ("hoàn thành", "huỷ", "trả hàng", "đang giao")


def order_key_roles(slots: dict | None) -> dict[str, dict[str, str]]:
    f = (slots or {}).get("order_key")
    return {f["table"]: {f["column"]: "order_id"}} if f else {}


def name_map(t: pd.DataFrame, short: str, full: str) -> dict[str, str]:
    """Folded short name -> full name. A short name written for two products is no abbreviation: dropped."""
    pairs = t[[short, full]].dropna()
    pairs = pairs[pairs[short].map(lambda v: isinstance(v, str))]
    out: dict[str, str] = {}
    seen: dict[str, set] = {}
    for s, f in zip(pairs[short], pairs[full]):
        k = fold(s).strip()
        seen.setdefault(k, set()).add(fold(f).strip())
        out.setdefault(k, str(f).strip())
    return {k: v for k, v in out.items() if len(seen[k]) == 1 and k != fold(v).strip()}


def apply_tables(tables: dict[str, pd.DataFrame], slots: dict | None) -> tuple[dict[str, pd.DataFrame], list[str]]:
    f = (slots or {}).get("name_map")
    if not f or f["line_table"] not in tables or f["table"] not in tables:
        return tables, []
    lines, col = tables[f["line_table"]], f["column"]
    if col not in lines.columns:
        return tables, []
    m = name_map(tables[f["table"]], f["short_col"], f["full_col"])
    new = lines[col].map(lambda v: m.get(fold(v).strip(), v) if isinstance(v, str) else v)
    changed = new.ne(lines[col]) & lines[col].notna()
    out = lines.copy()
    out[f"{col} (as written)"] = lines[col].where(changed)
    out[col] = new
    note = (f"product names written short in `{col}` ({int(changed.sum())} lines) read as the full name of "
            f"T['{f['table']}'].{f['full_col']} (short form in `{f['short_col']}`; `{col} (as written)` keeps the short form)")
    return {**tables, f["line_table"]: out}, [note]


def apply_lines(out: pd.DataFrame, slots: dict | None) -> list[str]:
    """Change `status` and `line_amount` of the canonical lines in place, before the revenue flags are derived."""
    notes = []
    st = (slots or {}).get("status_text")
    if st and st["column"] in out.columns:
        vmap = {fold(k).strip(): v for k, v in st["map"].items()}
        rank = out[st["column"]].map(lambda v: RANK.get(vmap.get(fold(v).strip())) if isinstance(v, str) else None)
        rank = pd.to_numeric(rank, errors="coerce")
        if "order_id" in out.columns:
            rank = rank.groupby(out["order_id"]).transform("max").where(out["order_id"].notna(), rank)
        label = rank.map({r: s for s, r in RANK.items()})
        out.loc[label.notna(), "status"] = label[label.notna()]
        said = ", ".join(f"{k!r} = {v}" for k, v in st["map"].items())
        notes.append(f"order status read from `{st['column']}` ({said}); an order takes the worst status of its lines")
    la = (slots or {}).get("line_amount")
    if la and {la["column"], "qty", "unit_price", "line_amount"} <= set(out.columns):
        d, q, p, a = (pd.to_numeric(out[c], errors="coerce") for c in (la["column"], "qty", "unit_price", "line_amount"))
        gross = q * p
        off = d if la["kind"] == "amount" else gross * d / 100
        rows = (d > 0) & ((gross - a).abs() <= 1)
        out.loc[rows, "line_amount"] = (gross - off)[rows]
        notes.append(f"line amounts were written before the line discount `{la['column']}` ({la['kind']}): "
                     f"taken off on {int(rows.sum())} lines")
    return notes
