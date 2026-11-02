"""Canonical sales lines built from column roles, with the rulebook's revenue filter precomputed.

One row per sold line: the line table (role=line_amount) joined with order-level fields (date,
status, customer, plus the header's other columns) from a header table sharing the order id, and
with product attributes (group, active ingredient, cost, ...) from product master tables.
`counted` applies the default revenue rule: the order is not cancelled, not returned, and has no
returned line ("đang giao" still counts). `build_orders` rolls the lines up to one row per order.
"""

import re

import pandas as pd

from datamate.ingest.roles import header_key, normalize_status
from datamate.text import fold

CANON_ROLES = ("order_id", "order_date", "order_ts", "status", "line_amount", "qty", "qty_base", "unit",
               "unit_price", "product_id", "product_name", "customer_id", "customer_name")
ORDER_ROLES = ("order_date", "order_ts", "status", "customer_id", "customer_name")
_WALKIN = re.compile(r"^(khach )?(le|vang lai|walk ?in|khong ten|khach)$|^(kh|k) ?le$|^kl$|^kvl$|^-+$")
_PHONE = re.compile(r"\s*[-–,(]?\s*\(?(?:\+?84|0)\d[\d .]{7,12}\)?\s*$")  # "Tên - 0328 548 455", "Tên (0913885954)"


def strip_phone(v):
    """Customer names sometimes carry the phone number: keep the name only."""
    return _PHONE.sub("", v).strip() if isinstance(v, str) else v


# product masters only: not imports, stock snapshots, lots or price history
_NOT_MASTER = re.compile(r"nhap|import|ncc|ton|kho|lo ?hang|lo$|gia|quy ?doi|the kho|chi ?tiet")


def is_walkin(names) -> pd.Series:
    """True for walk-in / anonymous customers (Khách lẻ, lẻ, kl, vãng lai, blank)."""
    s = pd.Series(names)
    return s.isna() | s.astype(str).map(lambda v: bool(_WALKIN.match(fold(v).strip(" .")))) | (s.astype(str).str.strip() == "")


def _role_cols(roles: dict[str, str]) -> dict[str, str]:
    return {r: c for c, r in roles.items()}


_JUNK = re.compile(r"hinh anh|barcode|ma vach|vi tri|trong luong|mo ta|mau ghi chu|url|dinh muc|ton (lon|nho) nhat"
                   r"|lien quan|thuoc tinh|^stt$|^so thu tu$|id cua hang|ma chi nhanh|^tao luc$|ban truc tiep|bao hanh")


def _extra_cols(df: pd.DataFrame, roles: dict[str, str], taken) -> list[str]:
    """Columns of df that carry no canonical role, are not already present and are not catalogue noise."""
    return [c for c in df.columns if c not in roles and not str(c).endswith("__norm") and c not in taken
            and not str(c).startswith("_") and not _JUNK.search(header_key(c))]


def _has_time(s: pd.Series) -> bool:
    t = pd.to_datetime(s, errors="coerce").dropna()
    return bool(len(t)) and bool((t != t.dt.normalize()).any())


_FACTOR = re.compile(r"^(ti le quy doi|he so( qd| quy doi)?|quy doi|bang)$")
_CONV_UNIT = re.compile(r"^(don vi quy doi|dv quy doi|dv nhap|dv lon|loai dv|quy cach|don vi( tinh)?|dvt|dv)$")
_BASE_UNIT = re.compile(r"^(don vi co ban|dv co ban|dv nho|dv goc|don vi goc)$")


def unit_factors(tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]], keys: dict[str, pd.Series],
                 skip: str) -> tuple[dict, str, str] | None:
    """(factors, key, description) from a unit-conversion table, or None; factors = {(folded product key, folded unit):
    base units per unit}.

    keys: the sales lines' product columns ({"product_id": ..., "product_name": ...}); the conversion table's key column is
    the one whose values match them best. A factor cell with its own unit ("15 vỉ") is resolved through the same
    product's row for that unit (hộp = 15 vỉ = 15 × 12 viên)."""
    for n, t in tables.items():
        if n == skip or "order_id" in roles_by_table.get(n, {}).values():
            continue
        heads = {c: header_key(c) for c in t.columns if not str(c).startswith("_")}
        fcol = next((c for c, h in heads.items() if _FACTOR.match(h) and pd.api.types.is_numeric_dtype(t[c])), None)
        ucol = next((c for c, h in heads.items() if _CONV_UNIT.match(h) and not _BASE_UNIT.match(h)
                     and roles_by_table.get(n, {}).get(c) != "base_unit"), None)
        if fcol is None or ucol is None:
            continue
        best = None
        for kname, kvals in keys.items():
            ref = set(kvals.dropna().astype(str).map(fold))
            for c in heads:
                if c in (fcol, ucol) or pd.api.types.is_numeric_dtype(t[c]):
                    continue
                vals = t[c].dropna().astype(str).map(fold)
                share = vals.isin(ref).mean() if len(vals) else 0.0
                if share > 0.8 and (best is None or share > best[2]):
                    best = (kname, c, share)
        if best is None:
            continue
        kname, kcol, _ = best
        sub = f"{fcol}__unit" if f"{fcol}__unit" in t.columns else None
        raw = {(fold(k), fold(u)): (float(f), fold(s) if sub and isinstance(s, str) else None)
               for k, u, f, s in zip(t[kcol], t[ucol], t[fcol], t[sub] if sub else [None] * len(t))
               if pd.notna(k) and pd.notna(u) and pd.notna(f)}
        factors = {}
        for (k, u), (f, s) in raw.items():
            seen = {u}
            while s and (k, s) in raw and s not in seen:  # nested: hộp -> vỉ -> viên
                seen.add(s)
                g, s = raw[(k, s)]
                f *= g
            factors[(k, u)] = f
        return factors, kname, f"T['{n}'] (`{fcol}` per `{ucol}`, keyed by {kname})"
    return None


def build_sales(tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]]) -> tuple[pd.DataFrame | None, str]:
    """Return (sales_lines frame or None, one-paragraph description for the prompt)."""
    cands = [n for n, r in roles_by_table.items()
             if "line_amount" in r.values() and n in tables and not re.search(r"nhap|import|ncc", fold(n))]
    if not cands:
        return None, ("sales_lines(): not built for this shop (no table with line amounts). Compute sales from the T tables directly; "
                      "this does not make sales questions unanswerable.")
    line_t = max(cands, key=lambda n: (("product_name" in roles_by_table[n].values()) or ("product_id" in roles_by_table[n].values()), len(tables[n])))
    lroles = roles_by_table[line_t]
    lr = _role_cols(lroles)
    L = tables[line_t]
    out = pd.DataFrame({r: L[c] for r, c in lr.items() if r in CANON_ROLES}, index=L.index)
    sources = [f"T['{line_t}']"]
    if "status" in lr and f"{lr['status']}__norm" in L.columns:
        out["status"] = L[f"{lr['status']}__norm"]
    extras = _extra_cols(L, lroles, out.columns)
    for c in extras:
        out[c] = L[c]

    if "order_id" in out.columns:
        missing = [r for r in ORDER_ROLES if r not in out.columns]
        ids = set(out["order_id"].dropna())
        best, best_score = None, (0, 0.0)
        for n, r in roles_by_table.items():
            rc = _role_cols(r)
            if n == line_t or n not in tables or "order_id" not in rc or n in cands:
                continue
            hid = tables[n][rc["order_id"]].dropna()
            if hid.nunique() < 0.95 * len(hid):
                continue  # several rows per order (lots, payments): not an order header
            overlap = hid.isin(ids).mean() if len(hid) else 0.0
            score = (sum(m in rc for m in ORDER_ROLES), overlap)  # prefer the real order header over payments etc.
            if overlap > 0.5 and score > best_score:
                best, best_score = n, score
        if best:
            hroles = roles_by_table[best]
            rc = _role_cols(hroles)
            H = tables[best]
            cols = {"order_id": H[rc["order_id"]]}
            for m in missing:
                if m in rc:
                    cols[m] = H[f"{rc[m]}__norm"] if m == "status" and f"{rc[m]}__norm" in H.columns else H[rc[m]]
            for c in _extra_cols(H, hroles, set(out.columns) | set(cols)):
                cols[c] = H[c]
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

    if "product_name" in out.columns and "product_id" not in out.columns:  # get codes, so masters keyed by code join
        for n, r in roles_by_table.items():
            rc = _role_cols(r)
            if n != line_t and n in tables and "product_id" in rc and "product_name" in rc and not _NOT_MASTER.search(fold(n)):
                P = tables[n].assign(__k=tables[n][rc["product_name"]].astype(str).map(fold)).drop_duplicates("__k")
                out["product_id"] = out["product_name"].astype(str).map(fold).map(P.set_index("__k")[rc["product_id"]])
                sources.append(f"T['{n}'] (product_id)")
                break

    if "customer_id" in out.columns and "customer_name" not in out.columns:
        for n, r in roles_by_table.items():
            rc = _role_cols(r)
            if n != line_t and n in tables and "customer_id" in rc and "customer_name" in rc and "order_id" not in rc:
                names = tables[n].drop_duplicates(rc["customer_id"]).set_index(rc["customer_id"])[rc["customer_name"]]
                out["customer_name"] = out["customer_id"].map(names)
                sources.append(f"T['{n}'] (customer_name)")
                break

    for n, r in roles_by_table.items():  # product attributes: group, active ingredient, cost, prescription ...
        rc = _role_cols(r)
        if n == line_t or n not in tables or "order_id" in rc or "line_amount" in rc or _NOT_MASTER.search(fold(n)):
            continue
        key = next((k for k in ("product_id", "product_name") if k in rc and k in out.columns), None)
        if key is None:
            continue
        P = tables[n]
        kcol = P[rc[key]].astype(str).map(fold) if key == "product_name" else P[rc[key]]
        if kcol.nunique() < 0.9 * kcol.notna().sum():
            continue  # not one row per product
        add = _extra_cols(P, r, out.columns)
        if not add:
            continue
        M = P[add].assign(__k=kcol.values).drop_duplicates("__k").set_index("__k")
        k = out[key].astype(str).map(fold) if key == "product_name" else out[key]
        for c in add:
            out[c] = k.map(M[c]).values
        sources.append(f"T['{n}'] (product attributes: {', '.join(add)})")

    if "order_date" not in out.columns and "order_ts" in out.columns:
        out["order_date"] = out["order_ts"]
    if "order_date" not in out.columns:
        return None, (f"sales_lines(): not built for this shop (no order date found for {sources[0]}). Compute sales from the T tables "
                      "directly; this does not make sales questions unanswerable.")
    if "order_ts" not in out.columns and _has_time(out["order_date"]):
        out["order_ts"] = pd.to_datetime(out["order_date"], errors="coerce")
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
        neg, out["is_goods"] = q < 0, q.notna()
    else:
        neg, out["is_goods"] = pd.to_numeric(out["line_amount"], errors="coerce") < 0, True
    if "qty_base" not in out.columns and "qty" in out.columns:
        out["qty_base"] = out["qty"]
        keys = {k: out[k] for k in ("product_id", "product_name") if k in out.columns}
        conv = unit_factors(tables, roles_by_table, keys, line_t) if "unit" in out.columns and keys else None
        if conv:  # rulebook: a unit without a conversion row is the base unit (factor 1)
            factors, kname, how = conv
            f = [factors.get((fold(k), fold(u)), 1.0) if pd.notna(k) and pd.notna(u) else 1.0
                 for k, u in zip(out[kname], out["unit"])]
            out["qty_base"] = pd.to_numeric(out["qty"], errors="coerce") * pd.Series(f, index=out.index)
            sources.append(f"qty_base = qty × unit factor ({how})")
    if "order_id" in out.columns:
        bad_orders = set(out.loc[bad | neg, "order_id"].dropna())
        out["order_ok"] = ~out["order_id"].isin(bad_orders) & ~bad
    else:
        out["order_ok"] = ~(bad | neg)
    out["counted"] = out["order_ok"] & ~neg & out["is_goods"]
    out = out.rename(columns={"line_amount": "amount"})
    if "customer_id" in out.columns or "customer_name" in out.columns:
        name = out.get("customer_name", out.get("customer_id"))
        out["walkin"] = is_walkin(name) & (out["customer_id"].isna() | is_walkin(out["customer_id"]) if "customer_id" in out.columns else True)
        if "customer_name" not in out.columns:
            out["customer_name"] = out["customer_id"]
        out["customer_name"] = out["customer_name"].map(strip_phone)
        cust = out["customer_id"].fillna(out["customer_name"]) if "customer_id" in out.columns else out["customer_name"]
        out["customer"] = cust.where(~out["walkin"])  # walk-ins have no customer key, so groupby drops them

    order = ["order_id", "date", "order_ts", "status", "counted", "order_ok", "is_goods", "amount", "qty", "qty_base",
             "unit", "unit_price", "product_id", "product_name", "customer", "customer_id", "customer_name", "walkin"]
    canon = [c for c in order if c in out.columns]
    extra = [c for c in out.columns if c not in canon and c != "order_date"]
    out = out[canon + extra].reset_index(drop=True)
    desc = (f"sales_lines(): {len(out)} rows built from {' + '.join(sources)}.\n"
            f"Canonical columns: {', '.join(canon)}.\n"
            f"Other columns (copied unchanged from the source tables; use these names exactly): {', '.join(map(str, extra)) or '-'}.\n"
            "`counted` = goods line of an order that is not cancelled/returned and has no returned line (default revenue rule; 'đang giao' counts). "
            "`order_ok` = the line's order is not cancelled/returned. `is_goods` = False for voucher/points/shipping lines. "
            "`customer` = customer key (code if the shop has codes, else name), empty for walk-in customers (Khách lẻ, kl, ...). "
            "`amount` = line amount after line discount. `order_ts` = order timestamp (use it for shifts). "
            "orders() rolls these lines up to one row per order.")
    return out, desc


def build_orders(sales: pd.DataFrame) -> pd.DataFrame:
    """One row per order: first value of every order-level column, plus `goods_amount` (Σ amount of goods lines),
    `n_lines`, `qty_base` (Σ over goods lines) and `counted` (= order not cancelled/returned)."""
    if "order_id" not in sales.columns:
        raise ValueError("this shop's sales lines have no order id")
    g = sales.groupby("order_id", sort=False, dropna=True)
    goods = sales[sales["is_goods"]]
    gg = goods.groupby("order_id", sort=False)
    line_level = {"amount", "qty", "qty_base", "unit", "unit_price", "product_id", "product_name", "is_goods", "counted"}
    first = g[[c for c in sales.columns if c not in line_level and c != "order_id"]].first()
    first["counted"] = first["order_ok"]
    first["goods_amount"] = gg["amount"].sum().reindex(first.index).fillna(0)
    first["n_lines"] = gg.size().reindex(first.index).fillna(0).astype(int)
    if "qty_base" in sales.columns:
        first["qty_base"] = gg["qty_base"].sum().reindex(first.index).fillna(0)
    return first.drop(columns=["order_ok"]).reset_index()
