"""Canonical sales lines built from column roles, with the rulebook's revenue filter precomputed.

One row per sold line: the line table (role=line_amount) joined with order-level fields (date,
status, customer) from a header table sharing the order id. `counted` applies the default
revenue rule: the order is not cancelled, not returned, and has no returned line ("đang giao"
still counts).
"""

import re

import pandas as pd

from datamate.ingest.roles import normalize_status
from datamate.text import fold

CANON_ROLES = ("order_id", "order_date", "order_ts", "status", "line_amount", "qty", "qty_base", "unit",
               "product_id", "product_name", "customer_id", "customer_name")
ORDER_ROLES = ("order_date", "order_ts", "status", "customer_id", "customer_name")
_WALKIN = re.compile(r"^(khach )?(le|vang lai|walk ?in|khong ten|khach)$|^kh le$|^-+$")


def is_walkin(names) -> pd.Series:
    """True for walk-in / anonymous customers (Khách lẻ, lẻ, vãng lai, blank)."""
    s = pd.Series(names)
    return s.isna() | s.astype(str).map(lambda v: bool(_WALKIN.match(fold(v).strip(" .")))) | (s.astype(str).str.strip() == "")


def _role_cols(roles: dict[str, str]) -> dict[str, str]:
    return {r: c for c, r in roles.items()}


def build_sales(tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]]) -> tuple[pd.DataFrame | None, str]:
    """Return (sales_lines frame or None, one-paragraph description for the prompt)."""
    cands = [n for n, r in roles_by_table.items()
             if "line_amount" in r.values() and n in tables and not re.search(r"nhap|import|ncc", fold(n))]
    if not cands:
        return None, "sales_lines(): not available (no table with line amounts); compute from T directly."
    line_t = max(cands, key=lambda n: (("product_name" in roles_by_table[n].values()) or ("product_id" in roles_by_table[n].values()), len(tables[n])))
    lr = _role_cols(roles_by_table[line_t])
    L = tables[line_t]
    out = pd.DataFrame({r: L[c] for r, c in lr.items() if r in CANON_ROLES}, index=L.index)
    sources = [f"T['{line_t}']"]
    if "status" in lr and f"{lr['status']}__norm" in L.columns:
        out["status"] = L[f"{lr['status']}__norm"]

    missing = [r for r in ORDER_ROLES if r not in out.columns]
    if missing and "order_id" in out.columns:
        ids = set(out["order_id"].dropna())
        best, best_overlap = None, 0.0
        for n, r in roles_by_table.items():
            rc = _role_cols(r)
            if n == line_t or n not in tables or "order_id" not in rc or not any(m in rc for m in missing):
                continue
            hid = tables[n][rc["order_id"]].dropna()
            overlap = hid.isin(ids).mean() if len(hid) else 0.0
            if overlap > best_overlap:
                best, best_overlap = n, overlap
        if best and best_overlap > 0.5:
            rc = _role_cols(roles_by_table[best])
            H = tables[best]
            cols = {"order_id": H[rc["order_id"]]}
            for m in missing:
                if m in rc:
                    cols[m] = H[f"{rc[m]}__norm"] if m == "status" and f"{rc[m]}__norm" in H.columns else H[rc[m]]
            hdr = pd.DataFrame(cols).drop_duplicates("order_id")
            out = out.merge(hdr, on="order_id", how="left")
            sources.append(f"T['{best}'] (order fields: {', '.join(c for c in cols if c != 'order_id')})")

    if "product_id" in out.columns and "product_name" not in out.columns:
        for n, r in roles_by_table.items():
            rc = _role_cols(r)
            if n != line_t and n in tables and "product_id" in rc and "product_name" in rc:
                names = tables[n].drop_duplicates(rc["product_id"]).set_index(rc["product_id"])[rc["product_name"]]
                out["product_name"] = out["product_id"].map(names)
                sources.append(f"T['{n}'] (product_name)")
                break

    if "order_date" not in out.columns and "order_ts" in out.columns:
        out["order_date"] = out["order_ts"]
    if "order_date" not in out.columns:
        return None, f"sales_lines(): not available (no order date found for {sources[0]}); compute from T directly."
    out["date"] = pd.to_datetime(out["order_date"], errors="coerce").dt.normalize()
    if "status" not in out.columns:  # rulebook: no status column -> status is written in the notes
        note = next((c for c in L.columns if fold(c).startswith("ghi chu") and L[c].notna().any()), None)
        if note is not None and len(out) == len(L):
            out["status"] = L[note].map(lambda v: "hoàn thành" if pd.isna(v) else normalize_status(v)).values
            sources.append(f"status parsed from T['{line_t}']['{note}']")
        else:
            out["status"] = "hoàn thành"
    bad = out["status"].isin(["huỷ", "trả hàng"])
    qty_col = "qty" if "qty" in out.columns else ("qty_base" if "qty_base" in out.columns else None)
    if qty_col:  # returns are negative quantities; lines without a quantity are vouchers / points / shipping
        q = pd.to_numeric(out[qty_col], errors="coerce")
        neg, out["item"] = q < 0, q.notna()
    else:
        neg, out["item"] = pd.to_numeric(out["line_amount"], errors="coerce") < 0, True
    if "order_id" in out.columns:
        bad_orders = set(out.loc[bad | neg, "order_id"].dropna())
        out["counted"] = ~(bad | neg) & ~out["order_id"].isin(bad_orders) & out["item"]
    else:
        out["counted"] = ~(bad | neg) & out["item"]
    out = out.rename(columns={"line_amount": "amount"})
    if "customer_id" in out.columns or "customer_name" in out.columns:
        name = out.get("customer_name", out.get("customer_id"))
        out["walkin"] = is_walkin(name) & (out["customer_id"].isna() if "customer_id" in out.columns else True)
        if "customer_name" not in out.columns:
            out["customer_name"] = out["customer_id"]
        cust = out["customer_id"].fillna(out["customer_name"]) if "customer_id" in out.columns else out["customer_name"]
        out["customer"] = cust.where(~out["walkin"])  # walk-ins have no customer key, so groupby drops them

    order = ["order_id", "date", "status", "counted", "item", "amount", "qty", "qty_base", "unit", "product_id",
             "product_name", "customer", "customer_id", "customer_name", "walkin", "order_ts"]
    out = out[[c for c in order if c in out.columns]].reset_index(drop=True)
    desc = (f"sales_lines(): {len(out)} rows built from {' + '.join(sources)}. Columns: {', '.join(out.columns)}. "
            f"`counted` = goods line of an order that is not cancelled/returned and has no returned line (default revenue rule; 'đang giao' counts); `item` = False for voucher/points/shipping lines. `customer` = customer key (code if the shop has codes, else name), empty for walk-in customers. "
            f"`amount` = line amount after line discount. Check that this matches the README before relying on it.")
    return out, desc
