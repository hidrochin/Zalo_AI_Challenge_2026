"""Shop audit (L0 + L2): cross-checks of a shop's ingested data that need no gold answer and no model call.

A shop of a known export style reads cleanly; a new style can fail in ways no value check sees (every number off by the
same few percent). Each flag compares two things the shop's own files say about the same fact: the README against the
roles, the status column against the notes, the line amount against quantity × price and discount, the order rows of
one order against each other. Thresholds hold on all 30 train shops (no train shop is flagged); `datamate audit`
prints the flags of a split. A `suspect` shop is the unit of risk for the decision (agent/gates.py `shop_gate`).
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from datamate.ingest.business import FIRST_ROW_NOTE, META_COLS, Business
from datamate.ingest.roles import header_key
from datamate.text import fold

# whole-cell status phrases, from the train shops' status columns (`khách hủy`, `Hoàn trả`, `Chờ giao`, `hủy đơn`...)
_STATUS_CELL = re.compile(r"^(da |khach |don )?(huy|huy don|cancel|cancelled|canceled)$|^(da |khach )?(tra hang|hoan tra|tra lai)$"
                          r"|^(dang|chua|cho) giao( hang)?$")
_LINE_DISCOUNT = re.compile(r"^(giam( gia)?|chiet khau|ck|km|khuyen mai|tien giam)( dong| hang| sp)?( \((d|vnd|%)\))?$")
_LINE_ROLES = {"qty", "qty_base", "line_amount", "unit_price", "product_name", "product_id", "unit", "base_unit", "line_cost"}
SUSPECT_FLAGS = ("role_gap", "status_in_text", "discount_unapplied", "fill_gap")


@dataclass
class Audit:
    level: str = "clean"  # clean | suspect
    flags: dict[str, str] = field(default_factory=dict)  # flag -> what was seen
    features: dict[str, float] = field(default_factory=dict)  # measured shares, flagged or not

    def to_json(self) -> dict:
        return asdict(self)

    def note(self) -> str:
        return "; ".join(f"{k}: {v}" for k, v in self.flags.items())


def desc_key(desc: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", fold(desc)).split())


def readme_roles(businesses: list[Business]) -> dict[str, str]:
    """README description -> role, over the columns of (train) shops that got a role: a description another shop shares
    word for word means the same column."""
    out: dict[str, str] = {}
    for biz in businesses:
        for ti in biz.info.values():
            for c, d in ti.descs.items():
                r = ti.roles.get(c)
                if r and d and len(desc_key(d).split()) >= 4:
                    out.setdefault(desc_key(d), r)
    return out


def _role_gap(biz: Business, known: dict[str, str]) -> str | None:
    gaps = [f"{name}.{c} (README as a {known[desc_key(d)]} column)" for name, ti in biz.info.items()
            for c, d in ti.descs.items() if d and c in biz.tables[name].columns and not ti.roles.get(c)
            and desc_key(d) in known and not re.search(r"nhap|import|ncc", fold(name))]  # sales tables only
    return ", ".join(gaps) or None


def _text_cells(s: pd.Series) -> pd.Series:
    return s.dropna().map(lambda v: fold(v).strip(" .!") if isinstance(v, str) else None).dropna()


def _status_in_text(biz: Business, sales: pd.DataFrame | None, feats: dict) -> str | None:
    """The canonical sales have no cancelled, returned or undelivered order, but a column without a role holds whole-cell
    status phrases on a share of the sales rows."""
    if sales is None or "status" not in sales or set(sales["status"].dropna().unique()) - {"hoàn thành"}:
        return None
    best = None
    for name, ti in biz.info.items():
        if not {"order_id", "line_amount"} & set(ti.roles.values()):
            continue
        t = biz.tables[name]
        for c in t.columns:
            if c in META_COLS or ti.roles.get(c) or str(c).endswith("__norm"):
                continue
            cells = _text_cells(t[c])
            hits = int(cells.map(lambda v: bool(_STATUS_CELL.match(v))).sum())
            share = hits / max(len(t), 1)
            if best is None or share > best[0]:
                best = (share, f"{name}.{c}: {hits} of {len(t)} rows say {', '.join(cells[cells.map(lambda v: bool(_STATUS_CELL.match(v)))].value_counts().index[:4])}")
    if best is None:
        return None
    feats["status_in_text"] = round(best[0], 4)
    return best[1] if best[0] >= 0.005 else None


def _discount_unapplied(sales: pd.DataFrame | None, feats: dict) -> str | None:
    """A line discount column, yet amount = quantity × unit price on lines that have a discount: the amount is before
    the discount (rulebook: revenue is after the line discount)."""
    if sales is None or not {"qty", "unit_price", "amount"} <= set(sales.columns):
        return None
    for c in sales.columns:
        if not _LINE_DISCOUNT.match(header_key(c)) or not pd.api.types.is_numeric_dtype(sales[c]):
            continue
        d = pd.to_numeric(sales[c], errors="coerce")
        if "order_id" in sales:  # one value on every line of an order: the order's discount, which revenue keeps
            multi = sales.groupby("order_id")[c].agg(["size", "nunique"])
            multi = multi[multi["size"] > 1]
            if len(multi) >= 30 and (multi["nunique"] <= 1).mean() >= 0.95:
                continue
        q, p, a = (pd.to_numeric(sales[k], errors="coerce") for k in ("qty", "unit_price", "amount"))
        rows = (d > 0) & q.notna() & p.notna() & a.notna() & (a != 0)
        if rows.sum() < 30:
            continue
        gross = ((q * p - a).abs() <= 1)[rows].mean()
        feats["discount_unapplied"] = round(float(gross), 4)
        if gross >= 0.95:
            return f"`{c}` > 0 on {int(rows.sum())} lines, and amount = qty × unit_price on {gross:.0%} of them"
    return None


def _fill_gap(biz: Business, feats: dict) -> str | None:
    """Rulebook §8: a blank cell on a later row of an order is the first row's value. An order-level column (one value
    per order) that is still blank on later rows while the order's first row has it: ingest did not fill it."""
    worst = None
    for name, ti in biz.info.items():
        done = next((n for n in ti.notes if n.startswith(FIRST_ROW_NOTE)), None)
        if done:  # ingest filled it already (business._group_fill / _block_fill): the layout is still one no train shop has
            feats["fill_gap"] = 1.0
            return f"{name}: {done}"
    for name, ti in biz.info.items():
        oid = next((c for c, r in ti.roles.items() if r == "order_id"), None)
        t = biz.tables[name]
        if len(t) < 50:
            continue
        if oid is None or oid not in t.columns:  # no order key: blocks of rows under a dated first row
            day = next((c for c, r in ti.roles.items() if r in ("order_date", "order_ts") and c in t.columns), None)
            if day is None or pd.isna(t[day].iloc[0]):
                continue
            data = [c for c in t.columns if c not in META_COLS and c != day]
            blank = t[day].isna() & t[data].notna().any(axis=1)
            share = float(blank.mean())
            if share > 0.05 and (worst is None or share > worst[0]):
                worst = (share, f"{name}.{day} blank on {share:.0%} of rows that carry data under a dated row")
            continue
        g = t.groupby(oid, sort=False)
        size = g[oid].transform("size")
        later = (g.cumcount() > 0) & (size > 1)
        if later.sum() < 20:
            continue
        for c in t.columns:
            if c == oid or c in META_COLS or ti.roles.get(c) in _LINE_ROLES or str(c).endswith("__norm"):
                continue
            first = g[c].transform("first")  # first non-null value of the order
            blank = t[c].isna() & later & first.notna()
            share = blank.sum() / later.sum()
            if share <= 0.05:
                continue
            vals = t[c].dropna()
            one_value = (g[c].nunique() <= 1).mean() if len(vals) else 0
            if one_value >= 0.99 and (worst is None or share > worst[0]):
                worst = (share, f"{name}.{c} blank on {share:.0%} of later order rows that the first row fills")
    if worst:
        feats["fill_gap"] = round(worst[0], 4)
    return worst[1] if worst else None


def _name_gap(biz: Business, sales: pd.DataFrame | None, feats: dict) -> None:
    """Share of goods lines whose product name is in no product list (a feature: names written another way)."""
    if sales is None or "product_name" not in sales:
        return
    lines = sales.loc[sales.get("is_goods", True) & sales["product_name"].notna(), "product_name"].map(fold)
    best = None
    for name, ti in biz.info.items():
        r = set(ti.roles.values())
        if "product_name" not in r or {"order_id", "line_amount"} & r or re.search(r"nhap|import|ncc", fold(name)):
            continue
        col = next(c for c, v in ti.roles.items() if v == "product_name")
        known = set(biz.tables[name][col].dropna().map(fold))
        miss = float((~lines.isin(known)).mean()) if len(lines) else 0.0
        best = miss if best is None else min(best, miss)
    if best is not None:
        feats["name_gap"] = round(best, 4)


def audit(biz: Business, sales: pd.DataFrame | None, known_descs: dict[str, str] | None = None) -> Audit:
    feats: dict[str, float] = {}
    flags = {k: v for k, v in (("role_gap", _role_gap(biz, known_descs or {})),
                               ("status_in_text", _status_in_text(biz, sales, feats)),
                               ("discount_unapplied", _discount_unapplied(sales, feats)),
                               ("fill_gap", _fill_gap(biz, feats))) if v}
    _name_gap(biz, sales, feats)
    return Audit("suspect" if set(flags) & set(SUSPECT_FLAGS) else "clean", flags, feats)


KNOWN_DESCS = Path(__file__).resolve().parents[2] / "assets" / "readme_roles.json"


def load_known(path: Path = KNOWN_DESCS) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
