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
from datamate.sql.dims import with_entity_keys
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


# Train + public: the busiest real customer holds at most 0.64% of orders and 3× the median; a walk-in label holds ~10%.
_BUSY_SHARE, _BUSY_VS_MEDIAN = 0.05, 20
_WALKIN_WORD = re.compile(r"\b(khach|kh|le|vang lai|quay)\b")


def busy_customers(names: pd.Series, walkin: pd.Series, orders: pd.Series | None) -> tuple[set[str], list[str]]:
    """Folded names with far too many orders for one customer: (walk-in labels unknown to _WALKIN, names to flag).

    A name counts as a label only with a walk-in word, because a real contractor can also dominate a small shop."""
    ids = orders if orders is not None else pd.Series(range(len(names)), index=names.index)
    known = pd.DataFrame({"name": names.map(fold, na_action="ignore"), "order": ids})[~walkin].dropna()
    counts = known.groupby("name")["order"].nunique()
    busy = counts[(counts >= _BUSY_SHARE * ids.nunique()) & (counts >= _BUSY_VS_MEDIAN * counts.median())]
    labels = {n for n in busy.index if _WALKIN_WORD.search(n)}
    return labels, sorted(set(busy.index) - labels)


def _role_cols(roles: dict[str, str]) -> dict[str, str]:
    return {r: c for c, r in roles.items()}


# Roles that only say what a column is: it stays in the lines.
_PLAIN_ROLES = (None, "order_goods", "ticket_amount", "order_total", "pay_method", "channel", "debt_note", "line_cost")
_JUNK = re.compile(r"hinh anh|barcode|ma vach|vi tri|trong luong|mo ta|mau ghi chu|url|dinh muc|ton (lon|nho) nhat"
                   r"|lien quan|thuoc tinh|^stt$|^so thu tu$|id cua hang|ma chi nhanh|^tao luc$|ban truc tiep|bao hanh")


def _extra_cols(df: pd.DataFrame, roles: dict[str, str], taken) -> list[str]:
    """Columns of df that carry no canonical role, are not already present and are not catalogue noise."""
    return [c for c in df.columns if roles.get(c) in _PLAIN_ROLES and not str(c).endswith("__norm") and c not in taken
            and not str(c).startswith("_") and not _JUNK.search(header_key(c))]


def _has_time(s: pd.Series) -> bool:
    t = pd.to_datetime(s, errors="coerce").dropna()
    return bool(len(t)) and bool((t != t.dt.normalize()).any())


_FACTOR = re.compile(r"^(ti le quy doi|he so( qd| quy doi)?|quy doi|bang)$")
_CONV_UNIT = re.compile(r"^(don vi quy doi|dv quy doi|dv nhap|dv lon|loai dv|quy cach|don vi( tinh)?|dvt|dv)$")
_BASE_UNIT = re.compile(r"^(don vi co ban|dv co ban|dv nho|dv goc|don vi goc)$")


def unit_factors(tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]], keys: dict[str, pd.Series],
                 skip: str) -> list[tuple[dict, str, str]]:
    """(factors, key, description) of every unit-conversion table; factors = {(folded product key, folded unit): base
    units per unit}. A shop can keep some conversions in a sheet and others in the README's prose.

    keys: the sales lines' product columns ({"product_id": ..., "product_name": ...}); the conversion table's key column is
    the one whose values match them best. A factor cell with its own unit ("15 vỉ") is resolved through the same
    product's row for that unit (hộp = 15 vỉ = 15 × 12 viên)."""
    found = []
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
        found.append((factors, kname, f"T['{n}'] (`{fcol}` per `{ucol}`, keyed by {kname})"))
    return found


_LOOKUP_ROWS = 5000
_ALIAS_HEAD = re.compile(r"ghi tat|viet tat|ten tat|ky hieu|alias")


def _text_cols(t: pd.DataFrame) -> list[str]:
    return [c for c in t.columns if not str(c).startswith("_") and not str(c).endswith(("__norm", "__unit"))
            and t[c].map(lambda v: isinstance(v, str)).mean() > 0.5]


def alias_map(tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]], names: pd.Series,
              skip: str) -> tuple[dict[str, str], str] | None:
    """({folded written name: full name}, source) from a lookup sheet that spells out the names the sales lines abbreviate.

    A sheet counts when it explains most names on the lines, some of them only through its short column, and the short
    column says so by its header or the full column's names appear on the lines too (a description column never does)."""
    seen = set(names.dropna().astype(str).map(fold))
    for n, t in tables.items():
        roles = roles_by_table.get(n, {})
        if n == skip or "order_id" in roles.values() or len(t) > _LOOKUP_ROWS:
            continue
        cols = _text_cols(t)
        for short in cols:
            for full in cols:
                pairs = t[[short, full]].dropna()
                if short == full or pairs[short].nunique() < 0.9 * len(pairs) or len(pairs) < 3:
                    continue
                s, f = set(pairs[short].map(fold)), set(pairs[full].map(fold))
                only_short = seen & (s - f)
                if len(only_short) < max(2, 0.05 * len(seen)) or len(seen & (s | f)) < 0.8 * len(seen):
                    continue
                if not (_ALIAS_HEAD.search(header_key(short)) or len(seen & (f - s)) >= 2):
                    continue
                written = {fold(a): b for a, b in zip(pairs[short], pairs[full])}
                return {**written, **{fold(b): b for b in pairs[full]}}, f"T['{n}'] (`{short}` -> `{full}`)"
    return None


_HONORIFIC = re.compile(r"^(a|anh|c|chi|em|e|tho|co|chu|bac)[. ]+")
_PERSON_HEAD = re.compile(r"ho ten|ho va ten|ten day du|ten (nhan vien|tho|nv)|^tho$|^nhan vien$")


def _given_name(v) -> str:
    return _HONORIFIC.sub("", fold(v)).strip()


def full_names(out: pd.DataFrame, tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]], skip: str) -> list[str]:
    """Write staff columns with the full names of a staff list ("A.Đức" -> "Lê Văn Đức"); returns the columns changed.

    A value is replaced only when one person on the list has that given name, so two staff named Đức stay as written."""
    done = []
    for n, t in tables.items():
        if n == skip or len(t) > 200 or set(roles_by_table.get(n, {}).values()) & {"order_id", "customer_id", "customer_name", "product_name"}:
            continue
        for c in _text_cols(t):
            names = t[c].dropna().astype(str)
            if not _PERSON_HEAD.search(header_key(c)) or names.map(lambda v: len(v.split()) >= 2).mean() < 0.8:
                continue
            given: dict[str, list[str]] = {}
            for v in names.unique():
                given.setdefault(fold(v).split()[-1], []).append(v)
            lookup = {g: v[0] for g, v in given.items() if len(v) == 1}
            for col in _text_cols(out):
                if col in CANON_ROLES or col in done:
                    continue
                vals = out[col].dropna().astype(str)
                if vals.empty or vals.map(lambda v: _given_name(v) in lookup or fold(v) in {fold(x) for x in names}).mean() < 0.8:
                    continue
                out[col] = out[col].map(lambda v: lookup.get(_given_name(v), v) if isinstance(v, str) else v)
                done.append(col)
    return done


_GROUP_HEAD = re.compile(r"^(nhom( khach( hang)?| kh)?|loai khach hang|hang khach|hang( kh| thanh vien)?)$")
_ORDER_GOODS_HEAD = re.compile(r"^(gia tri hang|tien hang|tong tien hang)$")


def _order_level(tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]]) -> tuple[dict, dict, str] | None:
    """A shop without line items: its order or ticket rows read as one line each (order's goods value, ticket's total).
    Rows without an order id are one ticket each, so they get an id of their own."""
    for n, r in roles_by_table.items():
        if n not in tables or re.search(r"nhap|import|ncc", fold(n)) or not {"order_date", "order_ts"} & set(r.values()):
            continue
        t = tables[n]
        role_of = {v: c for c, v in r.items()}
        amount = role_of.get("order_goods") or role_of.get("ticket_amount")
        amount = amount or next((c for c in t.columns if "order_id" in role_of and _ORDER_GOODS_HEAD.match(header_key(c))), None)
        if amount is None:
            continue
        roles = {**r, amount: "line_amount"}
        if "order_id" not in role_of:
            t = t.assign(**{"Mã phiếu": [f"R{i + 1}" for i in range(len(t))]})
            roles["Mã phiếu"] = "order_id"
        return {**tables, n: t}, {**roles_by_table, n: roles}, n
    return None


def build_sales(tables: dict[str, pd.DataFrame], roles_by_table: dict[str, dict[str, str]]) -> tuple[pd.DataFrame | None, str]:
    """Return (sales_lines frame or None, one-paragraph description for the prompt)."""
    cands = [n for n, r in roles_by_table.items()
             if "line_amount" in r.values() and n in tables and not re.search(r"nhap|import|ncc", fold(n))]
    level = None if cands else _order_level(tables, roles_by_table)
    if level:
        tables, roles_by_table, name = level
        cands = [name]
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

    if "product_name" in out.columns:
        alias = alias_map(tables, roles_by_table, out["product_name"], line_t)
        if alias:
            written, how = alias
            out["product_name_written"] = out["product_name"]
            out["product_name"] = out["product_name"].map(lambda v: written.get(fold(v), v) if isinstance(v, str) else v)
            sources.append(f"product_name spelled out from {how}")

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

    for n, r in roles_by_table.items():  # the customer's group, so a share needs no join that would drop walk-in orders
        rc = _role_cols(r)
        key = next((k for k in ("customer_id", "customer_name") if k in rc and k in out.columns), None)
        if n == line_t or n not in tables or key is None or "order_id" in rc or "customer_group" in out.columns:
            continue
        group = next((c for c in tables[n].columns if _GROUP_HEAD.match(header_key(c))), None)
        if group is not None:
            G = tables[n].dropna(subset=[rc[key]]).drop_duplicates(rc[key]).set_index(rc[key])[group]
            out["customer_group"] = out[key].map(G)
            sources.append(f"T['{n}'] (customer_group from `{group}`)")

    if "customer_group" not in out.columns:  # the group can sit on the order itself
        group = next((c for c in out.columns if _GROUP_HEAD.match(header_key(c))), None)
        if group is not None:
            out["customer_group"] = out[group]

    people = full_names(out, tables, roles_by_table, line_t)
    if people:
        sources.append(f"full staff names in {', '.join(people)}")
    if level:
        sources.append("one row per order or ticket: no line items, so no product or quantity")

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
        conv = unit_factors(tables, roles_by_table, keys, line_t) if "unit" in out.columns and keys else []
        if conv:  # rulebook: a unit without a conversion row is the base unit (factor 1)
            f = pd.Series(1.0, index=out.index)
            for factors, kname, how in reversed(conv):  # the first table wins a product both name
                found = [factors.get((fold(k), fold(u))) if pd.notna(k) and pd.notna(u) else None
                         for k, u in zip(out[kname], out["unit"])]
                f = pd.Series(found, index=out.index, dtype=float).fillna(f)
                sources.append(f"qty_base = qty × unit factor ({how})")
            out["qty_base"] = pd.to_numeric(out["qty"], errors="coerce") * f
    if "order_id" in out.columns:
        bad_orders = set(out.loc[bad | neg, "order_id"].dropna())
        out["order_ok"] = ~out["order_id"].isin(bad_orders) & ~bad
    else:
        out["order_ok"] = ~(bad | neg)
    out["counted"] = out["order_ok"] & ~neg & out["is_goods"]
    out = out.rename(columns={"line_amount": "amount"})
    flagged: list[str] = []
    if "customer_id" in out.columns or "customer_name" in out.columns:
        name = out.get("customer_name", out.get("customer_id"))
        out["walkin"] = is_walkin(name) & (out["customer_id"].isna() | is_walkin(out["customer_id"]) if "customer_id" in out.columns else True)
        labels, flagged = busy_customers(name, out["walkin"], out.get("order_id"))
        out["walkin"] |= name.map(fold, na_action="ignore").isin(labels)
        if "customer_name" not in out.columns:
            out["customer_name"] = out["customer_id"]
        out["customer_name"] = out["customer_name"].map(strip_phone)
        cust = out["customer_id"].fillna(out["customer_name"]) if "customer_id" in out.columns else out["customer_name"]
        out["customer"] = cust.where(~out["walkin"])  # walk-ins have no customer key, so groupby drops them
        if "customer_group" in out.columns:  # rulebook: a walk-in order belongs to no group, not even `lẻ`
            out["customer_group"] = out["customer_group"].where(~out["walkin"])
    out = with_entity_keys(out)
    if "customer_key" in out.columns:
        out["customer"] = out["customer_key"]

    order = ["order_id", "date", "order_ts", "status", "counted", "order_ok", "is_goods", "amount", "qty", "qty_base",
             "unit", "unit_price", "product_id", "product_key", "product_name", "customer", "customer_key", "customer_id",
             "customer_name", "customer_group", "walkin"]
    canon = [c for c in order if c in out.columns]
    extra = [c for c in out.columns if c not in canon and c != "order_date"]
    out = out[canon + extra].reset_index(drop=True)
    desc = (f"sales_lines(): {len(out)} rows built from {' + '.join(sources)}.\n"
            f"Canonical columns: {', '.join(canon)}.\n"
            f"Other columns (copied unchanged from the source tables; use these names exactly): {', '.join(map(str, extra)) or '-'}.\n"
            "`counted` = goods line of an order that is not cancelled/returned and has no returned line (default revenue rule; 'đang giao' counts). "
            "`order_ok` = the line's order is not cancelled/returned. `is_goods` = False for voucher/points/shipping lines. "
            "`customer` = customer key (code if the shop has codes, else name), empty for walk-in customers (Khách lẻ, kl, ...). "
            "`product_name` and `customer_name` hold ONE spelling per product or customer, so filtering or grouping on them "
            "covers every spelling in the files (`*_name_raw` keeps the spelling of each row). "
            "`amount` = line amount after line discount. `order_ts` = order timestamp (use it for shifts). "
            "orders() rolls these lines up to one row per order.")
    if flagged:
        desc += f"\nCheck before ranking customers: {', '.join(flagged)} hold(s) over 5% of orders, unusual for one customer."
    return out, desc


def build_orders(sales: pd.DataFrame) -> pd.DataFrame:
    """One row per order: first value of every order-level column, plus `goods_amount` (Σ amount of goods lines),
    `n_lines`, `qty_base` (Σ over goods lines) and `counted` (= order not cancelled/returned)."""
    if "order_id" not in sales.columns:
        raise ValueError("this shop's sales lines have no order id")
    g = sales.groupby("order_id", sort=False, dropna=True)
    goods = sales[sales["is_goods"]]
    gg = goods.groupby("order_id", sort=False)
    line_level = {"amount", "qty", "qty_base", "unit", "unit_price", "product_id", "product_key", "product_name",
                  "product_name_raw", "is_goods", "counted"}
    first = g[[c for c in sales.columns if c not in line_level and c != "order_id"]].first()
    first["counted"] = first["order_ok"]
    first["goods_amount"] = gg["amount"].sum().reindex(first.index).fillna(0)
    first["n_lines"] = gg.size().reindex(first.index).fillna(0).astype(int)
    if "qty_base" in sales.columns:
        first["qty_base"] = gg["qty_base"].sum().reindex(first.index).fillna(0)
    return first.drop(columns=["order_ok"]).reset_index()
