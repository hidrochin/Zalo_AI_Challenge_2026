"""Fact tables at the grain the rulebook counts in: one row per order that can owe, payment, delivery trip, import line,
expense, stock card row, lot.

The model is poor at compiling a multi-step rule into a query, so these definitions live here, tested against train
gold (`datamate facts-check`). Columns are found by their README description first, then by header, so a renamed column
in a new shop still resolves. A fact a shop does not record stays None, and its helpers report that.
"""

import re
from dataclasses import dataclass

import pandas as pd

from datamate.canon import build_orders, unit_factors
from datamate.ingest.business import META_COLS, Business, TableInfo
from datamate.ingest.roles import header_key
from datamate.text import fold

_DEBT_WORD = re.compile(r"ghi no|cong no|\bdebt\b|\bno\b")
_COD_WORD = re.compile(r"\bcod\b|thu ho")
_MARKET_WORD = re.compile(r"shopee|lazada|tiktok|tmdt|\bsan\b|sendo|tiki")


def pick(t: pd.DataFrame, ti: TableInfo, role: str, desc: str, head: str) -> str | None:
    """The column with this role (the model places what the rules cannot read), else the first whose README description
    matches desc, else the first whose header matches head."""
    placed = _role(ti, role)
    if placed in t.columns:
        return placed
    cols = [c for c in t.columns if c not in META_COLS and not str(c).endswith(("__norm", "__unit"))]
    found = next((c for c in cols if re.search(desc, fold(ti.descs.get(c, "")))), None)
    return found or next((c for c in cols if re.search(head, header_key(c))), None)


def _role(ti: TableInfo, role: str) -> str | None:
    return next((c for c, r in ti.roles.items() if r == role), None)


def _money(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def _day(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


@dataclass
class Facts:
    orders: pd.DataFrame | None = None  # order_id, date, status, customer, customer_name, walkin, total, on_credit, can_owe
    payments: pd.DataFrame | None = None  # order_id, date, amount
    deliveries: pd.DataFrame | None = None  # order_id, trip, date
    imports: pd.DataFrame | None = None  # doc, date, product_name, qty, unit, price, amount, qty_base
    expenses: pd.DataFrame | None = None  # date, amount
    stock_card: pd.DataFrame | None = None  # product_name, ts, balance
    stock_report: pd.DataFrame | None = None  # product_name, qty (base units, end of the data)
    lots: pd.DataFrame | None = None  # lot, product_name, expiry, qty, price
    lot_sales: pd.DataFrame | None = None  # lot, order_id, qty, returned
    units: pd.DataFrame | None = None  # product_name, unit, factor
    cost_col: str | None = None  # column of sales_lines with the cost of one unit sold
    price_list: pd.DataFrame | None = None  # product_name, date, price (the shop's price list: each row a price from that day)
    catalog_cost: pd.DataFrame | None = None  # product_name, cost (cost of one base unit in the product list)


def _order_fields(biz: Business) -> dict[str, pd.Series]:
    """order id -> value, for what sits on an order but not on the sales lines' canonical columns."""
    wanted = {
        "total": ("order_total", r"^tong tien don|^so khach phai tra|^tong tien cua ca don|^tong thanh toan", r"^(tong sau giam gia|tong don|tong bill|phai thu|tong thanh toan|tong tien don|tong tien)$"),
        "method": ("pay_method", r"^hinh thuc thanh toan", r"^(phuong thuc( tt| thanh toan)?|tra bang|hinh thuc( tt| thanh toan)|httt|pttt)$"),
        "channel": ("channel", r"^kenh( ban)?\b", r"^(kenh ban|ban qua|loai ban|nguon don)$"),
    }
    out: dict[str, pd.Series] = {}
    for name, t in biz.tables.items():
        ti = biz.info[name]
        oid = _role(ti, "order_id")
        if oid is None or re.search(r"nhap|import|ncc", fold(name)):
            continue
        for key, (role, desc, head) in wanted.items():
            c = pick(t, ti, role, desc, head)
            if key not in out and c is not None and ti.roles.get(c) != "line_amount":
                out[key] = t.dropna(subset=[oid]).drop_duplicates(oid).set_index(oid)[c]
    return out


def _orders(biz: Business, sales: pd.DataFrame) -> pd.DataFrame | None:
    fields = _order_fields(biz)
    if "total" not in fields or "order_id" not in sales.columns:
        return None
    all_orders = build_orders(sales)
    keep = [c for c in ("order_id", "date", "status", "customer", "customer_name", "walkin") if c in all_orders.columns]
    o = all_orders[keep].copy()
    o["total"] = _money(o["order_id"].map(fields["total"]))
    method = o["order_id"].map(fields["method"]).map(fold, na_action="ignore").fillna("") if "method" in fields else pd.Series("", index=o.index)
    channel = o["order_id"].map(fields["channel"]).map(fold, na_action="ignore").fillna("") if "channel" in fields else pd.Series("", index=o.index)
    cod = method.map(lambda v: bool(_COD_WORD.search(v)))
    market = channel.map(lambda v: bool(_MARKET_WORD.search(v)))
    o["on_credit"] = method.map(lambda v: bool(_DEBT_WORD.search(v))) & ~cod & (o["status"] != "huỷ")
    o["can_owe"] = ~cod & ~market & (o["status"] != "huỷ")  # rulebook: COD and marketplace orders are never debt
    return o


def _payments(biz: Business, order_ids: set) -> pd.DataFrame | None:
    """A table of money received per order. Known by its shape, since its headers vary: it points at orders, it has one date
    and one positive money column, and it describes neither goods nor the order itself."""
    for name, t in biz.tables.items():
        ti = biz.info[name]
        oid = _role(ti, "order_id")
        own = {"line_amount", "product_id", "product_name", "status", "customer_id", "customer_name"}
        if oid is None or own & set(ti.roles.values()) or re.search(r"nhap|import|ncc", fold(name)) or t[oid].isin(order_ids).mean() < 0.5:
            continue
        cols = [c for c in t.columns if c not in META_COLS and c != oid and not str(c).endswith(("__norm", "__unit"))]
        dates = [c for c in cols if pd.api.types.is_datetime64_any_dtype(t[c])]
        money = [c for c in cols if pd.api.types.is_numeric_dtype(t[c]) and (t[c].dropna() > 0).mean() > 0.9]
        when = pick(t, ti, "pay_date", r"^thoi diem thu|^ngay thu", r"^(thoi gian thu|ngay thu|ngay gio thu|thoi diem|thoi gian)$") or (dates[0] if dates else None)
        amount = pick(t, ti, "pay_amount", r"^so tien$", r"^(so tien( thu)?|gia tri|tien)$") or (money[0] if len(money) == 1 else None)
        if amount is None or when is None:
            continue
        return pd.DataFrame({"order_id": t[oid], "date": _day(t[when]), "amount": _money(t[amount])}).dropna(subset=["order_id", "date"])
    return None


def _deliveries(biz: Business) -> pd.DataFrame | None:
    for name, t in biz.tables.items():
        ti = biz.info[name]
        oid = _role(ti, "order_id")
        trip = pick(t, ti, "trip", r"^dot giao", r"^(luot giao|lan giao|dot giao|dot)$")
        when = pick(t, ti, "delivery_date", r"^ngay giao", r"^ngay (giao|cho)( hang)?$")
        if oid is None or trip is None or when is None:
            continue
        return pd.DataFrame({"order_id": t[oid], "trip": t[trip], "date": _day(t[when])}).dropna(subset=["order_id", "date"])
    return None


def _product_lookup(sales: pd.DataFrame) -> tuple[dict, dict]:
    """(code -> display name, folded raw name -> display name)."""
    by_code = {}
    if "product_id" in sales.columns:
        by_code = sales.dropna(subset=["product_id"]).drop_duplicates("product_id").set_index("product_id")["product_name"].to_dict()
    raw = sales["product_name_raw"] if "product_name_raw" in sales.columns else sales["product_name"]
    return by_code, {fold(a): b for a, b in zip(raw, sales["product_name"]) if isinstance(a, str)}


def _product_names(t: pd.DataFrame, ti: TableInfo, lookup: tuple[dict, dict]) -> pd.Series | None:
    by_code, by_name = lookup
    code, name = _role(ti, "product_id"), _role(ti, "product_name")
    if code is not None:
        return t[code].map(lambda v: by_code.get(str(v).strip(), by_code.get(v)))
    if name is not None:
        return t[name].map(lambda v: by_name.get(fold(v), v) if isinstance(v, str) else v)
    for c in t.columns:  # no role: the column whose values are the shop's products or codes
        if c in META_COLS or str(c).endswith(("__norm", "__unit")):
            continue
        vals = t[c].dropna().astype(str)
        if len(vals) and vals.map(lambda v: fold(v) in by_name).mean() > 0.8:
            return t[c].map(lambda v: by_name.get(fold(v), v) if isinstance(v, str) else v)
        if len(vals) and vals.map(lambda v: v.strip() in by_code).mean() > 0.8:
            return t[c].map(lambda v: by_code.get(str(v).strip()))
    return None


_SUPPLIER_HEAD = r"^(nha cung cap|ncc|ten ncc|ma ncc|ma nha cung cap|nha cc)$"
_NAME_HEAD = re.compile(r"^(ten( goi| ncc| nha cung cap)?|nha cung cap)$")


def _supplier_names(biz: Business, col: str, codes: pd.Series) -> pd.Series:
    """Supplier codes written out with the name of a supplier list that keys them (MaNhaCungCap -> TenGoi); names stay."""
    for name, t in biz.tables.items():
        if col not in t.columns or t[col].duplicated().any() or len(t) > 5000:
            continue
        full = next((c for c in t.columns if c != col and _NAME_HEAD.match(header_key(c))), None)
        if full is not None and codes.dropna().isin(t[col]).mean() > 0.8:
            return codes.map(dict(zip(t[col], t[full]))).fillna(codes)
    return codes


def _imports(biz: Business, lookup: tuple[dict, dict]) -> pd.DataFrame | None:
    for name, t in biz.tables.items():
        ti = biz.info[name]
        if not (re.search(r"nhap|import", fold(name)) or "import_price" in ti.roles.values()):
            continue
        price = pick(t, ti, "import_price", r"^gia nhap", r"^(gia nhap|don gia nhap|don gia)$")
        qty = pick(t, ti, "import_qty", r"^so luong nhap", r"^(sl nhap|so luong nhap|sl|so luong)$")
        if price is None or qty is None:
            continue
        doc = pick(t, ti, "import_doc", r"^ma phieu nhap", r"^(ma (nhap hang|phieu nhap|don nhap)|so phieu)$")
        unit = _role(ti, "unit") or pick(t, ti, "import_unit", r"^don vi nhap", r"^(dvt|dvt nhap|don vi tinh)$")
        f = pd.DataFrame({"doc": t[doc] if doc else None, "qty": pd.to_numeric(t[qty], errors="coerce"), "price": pd.to_numeric(t[price], errors="coerce"),
                          "unit": t[unit] if unit else None})
        f["product_name"] = _product_names(t, ti, lookup)
        when = pick(t, ti, "import_date", r"^ngay nhap", r"^(ngay nhap|ngay|thoi gian)$")
        f["date"], f["cancelled"] = (_day(t[when]) if when else pd.NaT), False
        sup = pick(t, ti, "supplier", r"^nha cung cap", _SUPPLIER_HEAD)
        f["supplier"] = t[sup].values if sup else None
        status = next((c for c in t.columns if str(c).endswith("__norm")), None)
        if status:
            f["cancelled"] = t[status] == "huỷ"
        if doc:  # date and status can sit on the receipt header
            for hname, h in biz.tables.items():
                hi = biz.info[hname]
                if hname == name or doc not in h.columns or h[doc].duplicated().any():
                    continue
                hd = pick(h, hi, "import_date", r"^ngay nhap", r"^(ngay nhap|ngay|thoi gian)( kho)?$")
                hs = next((c for c in h.columns if str(c).endswith("__norm")), None)
                hsup = None if sup else pick(h, hi, "supplier", r"^nha cung cap", _SUPPLIER_HEAD)
                head = h.set_index(doc)
                if hd:
                    f["date"] = f["date"].fillna(f["doc"].map(_day(head[hd])))
                if hs:
                    f["cancelled"] = f["cancelled"] | (f["doc"].map(head[hs]) == "huỷ")
                if hsup:
                    f["supplier"] = f["doc"].map(head[hsup])
                    sup = hsup
        if sup:
            f["supplier"] = _supplier_names(biz, sup, f["supplier"])
        f = f[~f["cancelled"]].dropna(subset=["date", "qty", "price"]).drop(columns="cancelled")
        return f.assign(amount=f["qty"] * f["price"]).reset_index(drop=True)
    return None


def _expenses(biz: Business) -> pd.DataFrame | None:
    for name, t in biz.tables.items():
        ti = biz.info[name]
        if not (re.search(r"chi ?phi|expense", fold(name)) or "expense_amount" in ti.roles.values()):
            continue
        amount = pick(t, ti, "expense_amount", r"^so tien", r"^(so tien( chi)?|thanh tien|tien|gia tri|chi phi)$")
        when = pick(t, ti, "expense_date", r"^ngay", r"^(ngay( chi| mua| phat sinh)?|thoi gian)$")
        if amount and when:
            return pd.DataFrame({"date": _day(t[when]), "amount": _money(t[amount])}).dropna(subset=["date"])
    return None


def _stock(biz: Business, lookup: tuple[dict, dict]) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    card = report = None
    for name, t in biz.tables.items():
        ti = biz.info[name]
        balance = pick(t, ti, "stock_balance", r"^ton (sau|cuoi)", r"^(ton cuoi|ton cuoi ky|ton thuc|ton kho|ton)$")
        if balance is None:
            continue
        names = _product_names(t, ti, lookup)
        if names is None:
            continue
        when = pick(t, ti, "stock_time", r"^(thoi diem|ngay gio)", r"^(ngay gio phat sinh|thoi gian|ngay)$")
        if when and card is None:
            card = pd.DataFrame({"product_name": names, "ts": pd.to_datetime(t[when], errors="coerce"), "balance": _money(t[balance]), "seq": range(len(t))}).dropna(subset=["product_name", "ts"])
        elif report is None:
            report = pd.DataFrame({"product_name": names, "qty": _money(t[balance])}).dropna(subset=["product_name"])
    return card, report


_BASE_QTY = re.compile(r"co ban|quy doi|quy ra le")


def _lots(biz: Business, lookup: tuple[dict, dict]) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Lot sales count base units, so the lot list must too: a list kept in base units wins over one in import units
    (a hand-kept import book with lot and expiry), which then only lends its price, brought to the base unit."""
    sold = None
    found: list[tuple[pd.DataFrame, bool]] = []  # (lots, quantity in base units)
    for name, t in biz.tables.items():
        ti = biz.info[name]
        lot = pick(t, ti, "lot_id", r"^so lo", r"^(lo|ma lo|so lo)$")
        if lot is None:
            continue
        oid = _role(ti, "order_id")
        expiry = pick(t, ti, "lot_expiry", r"^han su dung", r"^(hsd|han su dung|han dung)$")
        if oid is not None:
            q = pick(t, ti, "lot_sold", r"^so luong ban lay tu lo", r"^(sl quy ra le|so luong( \(dvt co ban\))?|sl quy doi)$")
            back = pick(t, ti, "lot_returned", r"^so luong khach tra", r"^(sl tra( \(dvt co ban\))?|so luong tra( \(dvt co ban\))?|sl tra quy doi)$")
            if q:
                sold = pd.DataFrame({"lot": t[lot], "order_id": t[oid], "qty": _money(t[q]), "returned": _money(t[back]) if back else 0.0})
        elif expiry is not None:
            q = pick(t, ti, "lot_qty", r"^so luong nhap \(don vi co ban\)", r"^(sl nhap( quy doi| \(dvt co ban\))?|so luong( nhap)?( \(dvt co ban\))?)$")
            price = pick(t, ti, "import_price", r"^gia nhap", r"^(don gia nhap|gia nhap)$")
            names = _product_names(t, ti, lookup)
            if names is None:
                code = next((c for c in t.columns if re.search(r"^ma (mon|hang|hh|sp|mat hang)", header_key(c))), None)
                names = t[code].map(lambda v: lookup[0].get(str(v).strip())) if code else None
            if q and names is not None:
                found.append((pd.DataFrame({"lot": t[lot], "product_name": names, "expiry": _day(t[expiry]), "qty": _money(t[q]),
                                            "price": _money(t[price]) if price else None}), bool(_BASE_QTY.search(header_key(q)))))
    if not found:
        return None, sold
    base = [f for f, b in found if b]
    if not base:
        return found[-1][0], sold
    lots = base[0]
    if lots["price"].isna().all():
        other = next((f for f, b in found if not b and f["price"].notna().any()), None)
        if other is not None:  # import-unit price × import-unit qty = the lot's cost, per base unit
            cost = (other["qty"] * other["price"]).groupby(other["lot"].astype(str)).sum()
            lots = lots.assign(price=lots["lot"].astype(str).map(cost) / lots["qty"].where(lots["qty"] > 0))
    return lots, sold


_PRICE_TABLE = re.compile(r"gia|price")
_PRICE_HEAD = r"^(gia moi|gia ban( le| moi)?|gia ap dung|gia|don gia( ban)?)$"
_PRICE_DATE = r"^(ngay( ap dung| doi( gia)?| thay doi| hieu luc| bat dau)?|ap dung tu|thoi gian)$"


def _price_list(biz: Business, lookup: tuple[dict, dict]) -> pd.DataFrame | None:
    """The shop's price list (bảng giá, lịch sử giá): a table, not a sale or import, with a product, a start day and a
    price, several rows per product. Rulebook: a price increase is a day whose new price is above the one before it."""
    for name, t in biz.tables.items():
        ti = biz.info[name]
        if not _PRICE_TABLE.search(fold(name)) or re.search(r"nhap|import|ncc", fold(name)) or {"order_id", "line_amount"} & set(ti.roles.values()):
            continue
        price = pick(t, ti, "list_price", r"^gia (ban|moi|ap dung)", _PRICE_HEAD)
        when = pick(t, ti, "price_date", r"^ngay (ap dung|doi|hieu luc)", _PRICE_DATE)
        names = _product_names(t, ti, lookup)
        if price is None or when is None or names is None:
            continue
        f = pd.DataFrame({"product_name": names, "date": _day(t[when]), "price": pd.to_numeric(t[price], errors="coerce")})
        f = f.dropna()
        if len(f) and f["product_name"].duplicated().any():
            return f.sort_values(["product_name", "date"]).reset_index(drop=True)
    return None


def _cost_col(biz: Business, sales: pd.DataFrame) -> str | None:
    """Cost of one unit sold, as recorded on the sale line. A catalogue cost is not it (rulebook: Lãi gộp)."""
    for name, t in biz.tables.items():
        ti = biz.info[name]
        if "line_amount" not in ti.roles.values():
            continue
        c = pick(t, ti, "line_cost", r"^gia von", r"^gia von( luc ban)?$")
        if c in sales.columns:
            return c
    return None


def _catalog_cost(biz: Business, lookup: tuple[dict, dict]) -> pd.DataFrame | None:
    """Cost of one base unit as the product list records it (rulebook: Giá trị hàng tồn kho): a table that is no sale,
    import or lot list, one row per product, with a cost column. The cost on a sale line is another figure."""
    for name, t in biz.tables.items():
        ti = biz.info[name]
        if re.search(r"nhap|import|ncc|lo hang", fold(name)) or {"order_id", "line_amount", "lot_id"} & set(ti.roles.values()):
            continue
        cost = pick(t, ti, "catalog_cost", r"^gia von", r"^(gia von( hien tai| hang| co ban)?|giavonhang|von)$")
        names = _product_names(t, ti, lookup)
        if cost is None or names is None:
            continue
        f = pd.DataFrame({"product_name": names, "cost": _money(t[cost])}).dropna()
        if len(f) and not f["product_name"].duplicated().any():
            return f.reset_index(drop=True)
    return None


def _units(biz: Business, sales: pd.DataFrame) -> pd.DataFrame | None:
    keys = {k: sales[k] for k in ("product_id", "product_name") if k in sales.columns}
    roles = {n: ti.roles for n, ti in biz.info.items()}
    rows = []
    names = {fold(v): v for v in sales["product_name"].dropna().astype(str)}
    by_code = _product_lookup(sales)[0]
    folded_codes = {fold(k): v for k, v in by_code.items()}
    for factors, kname, _ in unit_factors(biz.tables, roles, keys, ""):
        for (k, unit), f in factors.items():
            name = folded_codes.get(k) if kname == "product_id" else names.get(k)
            if name:
                rows.append((name, unit, f))
    return pd.DataFrame(rows, columns=["product_name", "unit", "factor"]).drop_duplicates(["product_name", "unit"]) if rows else None


def build_facts(biz: Business, sales: pd.DataFrame | None) -> Facts:
    f = _read_facts(biz, sales)
    for name, table in vars(f).items():
        if isinstance(table, pd.DataFrame) and table.empty:  # no usable row is no fact, not a total of 0
            setattr(f, name, None)
    return f


def _read_facts(biz: Business, sales: pd.DataFrame | None) -> Facts:
    f = Facts(deliveries=_deliveries(biz), expenses=_expenses(biz))
    if sales is None:
        return f
    lookup = _product_lookup(sales) if "product_name" in sales.columns else ({}, {})
    f.orders = _orders(biz, sales)
    if f.orders is not None:
        f.payments = _payments(biz, set(f.orders["order_id"]))
        if f.payments is not None and not f.orders["on_credit"].any():  # no payment method recorded: what the day of purchase left unpaid
            paid = f.payments.merge(f.orders[["order_id", "date"]].rename(columns={"date": "ordered"}), on="order_id")
            same_day = paid[paid["date"] <= paid["ordered"]].groupby("order_id")["amount"].sum()
            unpaid = f.orders["total"] - f.orders["order_id"].map(same_day).fillna(0.0)
            f.orders["on_credit"] = (unpaid > 0.5) & f.orders["can_owe"]
    if "product_name" not in sales.columns:
        return f
    f.units = _units(biz, sales)
    f.imports = _imports(biz, lookup)
    if f.imports is not None:
        f.imports = with_base_qty(f.imports, f.units)
    f.stock_card, f.stock_report = _stock(biz, lookup)
    f.lots, f.lot_sales = _lots(biz, lookup)
    f.cost_col = _cost_col(biz, sales)
    f.price_list = _price_list(biz, lookup)
    f.catalog_cost = _catalog_cost(biz, lookup)
    return f


def with_base_qty(imports: pd.DataFrame, units: pd.DataFrame | None) -> pd.DataFrame:
    """Import quantities in base units: a unit without a conversion row is the base unit."""
    out = imports.copy()
    factor = 1.0
    if units is not None:
        lookup = {(n, u): v for n, u, v in units.itertuples(index=False)}
        factor = [lookup.get((n, fold(u)), 1.0) for n, u in zip(out["product_name"], out["unit"])]
    out["qty_base"] = out["qty"] * factor
    return out


def missing(f: Facts) -> list[str]:
    """Facts this shop did not yield: it may not record them, or may name them in a way the rules cannot read."""
    gaps = [n for n in ("orders", "payments", "deliveries", "imports", "expenses", "lots", "lot_sales", "cost_col") if getattr(f, n) is None]
    return gaps + (["stock"] if f.stock_card is None and f.stock_report is None else [])


# ---------------------------------------------------------------- definitions (rulebook §4, §5)

def debt_rows(f: Facts, x: pd.Timestamp) -> pd.DataFrame:
    """Orders still owing at the end of day x, with `debt` and `age` (days from the order date)."""
    o = f.orders[(f.orders["date"] <= x) & f.orders["can_owe"]].copy()
    paid = f.payments[f.payments["date"] <= x].groupby("order_id")["amount"].sum()
    o["debt"] = o["total"] - o["order_id"].map(paid).fillna(0.0)
    o = o[o["debt"] > 0.5]
    return o.assign(age=(x - o["date"]).dt.days)


def _credit(f: Facts, customer=None) -> pd.DataFrame:
    o = f.orders[f.orders["on_credit"]]
    return o if customer is None else o[o["customer"] == customer]


def repaid(f: Facts, a: pd.Timestamp, b: pd.Timestamp, customer=None) -> float:
    """Money paid in a..b, after the day of purchase, on any on-credit order."""
    o = _credit(f, customer)[["order_id", "date"]].rename(columns={"date": "ordered"})
    p = f.payments.merge(o, on="order_id")
    return float(p.loc[(p["date"] >= a) & (p["date"] <= b) & (p["date"] > p["ordered"]), "amount"].sum())


def new_debt(f: Facts, a: pd.Timestamp, b: pd.Timestamp, customer=None) -> float:
    """Part of the on-credit orders placed in a..b left unpaid at the end of the day of purchase."""
    o = _credit(f, customer)
    o = o[(o["date"] >= a) & (o["date"] <= b)]
    p = f.payments.merge(o[["order_id", "date"]].rename(columns={"date": "ordered"}), on="order_id")
    same_day = p[p["date"] <= p["ordered"]].groupby("order_id")["amount"].sum()
    return float((o["total"] - o["order_id"].map(same_day).fillna(0.0)).clip(lower=0).sum())


def stock_of(f: Facts, sales: pd.DataFrame, x: pd.Timestamp) -> pd.Series:
    """Stock per product at the end of day x, in base units: the last stock card row, or the report at the end of the data
    rolled back by what moved after x."""
    if f.stock_card is not None:
        c = f.stock_card[f.stock_card["ts"].dt.normalize() <= x].sort_values(["ts", "seq"], kind="mergesort")
        return c.groupby("product_name")["balance"].last()
    if f.stock_report is None:
        if f.lots is None or f.lot_sales is None:
            raise LookupError("this shop records no stock")
        return lot_stock(f, sales, x).groupby("product_name")["left"].sum()
    qty = f.stock_report.groupby("product_name")["qty"].sum()
    sold = sales[sales["order_ok"] & sales["is_goods"] & (sales["date"] > x)].groupby("product_name")["qty_base"].sum()
    out = qty.add(sold, fill_value=0.0)
    if f.imports is not None:
        came = f.imports[f.imports["date"] > x].groupby("product_name")["qty_base"].sum()
        out = out.sub(came, fill_value=0.0)
    return out


def lot_stock(f: Facts, sales: pd.DataFrame, x: pd.Timestamp) -> pd.DataFrame:
    """Lots with what is left of each at the end of day x (`left`), sold lots of cancelled orders put back."""
    lots = f.lots.copy()
    s = f.lot_sales
    ordered = sales.dropna(subset=["order_id"]).drop_duplicates("order_id").set_index("order_id")
    when, cancelled = s["order_id"].map(ordered["date"]), s["order_id"].map(ordered["status"]) == "huỷ"
    out = s[(when <= x) & ~cancelled]
    moved = (out["qty"] - out["returned"]).groupby(out["lot"]).sum()
    lots["left"] = lots["qty"] - lots["lot"].map(moved).fillna(0.0)
    return lots
