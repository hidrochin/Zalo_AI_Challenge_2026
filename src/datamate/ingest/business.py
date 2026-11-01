"""Load one business folder into cleaned, typed pandas tables (cached as pickle)."""

import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from datamate.ingest.parsers import _is_null, is_explicit_date, parse_date, parse_number, parse_unit, year_month_hint
from datamate.ingest.readme import Readme, parse_readme
from datamate.ingest.roles import assign_roles, normalize_status
from datamate.ingest.tables import DATA_SUFFIXES, RawSheet, extract_table, read_file
from datamate.text import fold

INGEST_VERSION = 6
META_COLS = ("_file", "_sheet")

_DATE_NAME = re.compile(r"\b(ngay|date|thoi gian|time|thoi diem|hsd|han su dung|han dung|gio|sinh nhat|ngay sinh)\b")
_CODE_NAME = re.compile(
    r"\b(ma|id|sdt|so dt|dien thoai|phone|di dong|imei|cccd|cmnd|mst|ma so thue|bien so|barcode|ean|so ct|so phieu|so hd|so hoa don|lo)\b"
)
_GENERIC_SHEET = re.compile(r"^(sheet\d*|trang\d*|t?h?\w{0,5}\s*\d{1,2}\D{1,3}20\d{2}|\d{1,2}\D20\d{2})$", re.I)


@dataclass
class TableInfo:
    name: str
    sources: list[tuple[str, str | None]]
    descs: dict[str, str]
    roles: dict[str, str]
    notes: list[str] = field(default_factory=list)


@dataclass
class Business:
    id: str
    split: str
    readme: Readme
    tables: dict[str, pd.DataFrame]
    info: dict[str, TableInfo]
    warnings: list[str]
    load_seconds: float = 0.0
    version: int = INGEST_VERSION


def load_calendar(split_dir: Path) -> pd.DataFrame:
    cal = pd.read_csv(split_dir / "calendar.csv")
    cal["date"] = pd.to_datetime(cal["date"])
    return cal


def load_business(split_dir: Path, biz_id: str, cache_dir: Path | None = None, rebuild: bool = False) -> Business:
    split_dir = Path(split_dir)
    cache = Path(cache_dir) / split_dir.name / f"{biz_id}.pkl" if cache_dir else None
    if cache and cache.exists() and not rebuild:
        with cache.open("rb") as f:
            biz = pickle.load(f)
        if getattr(biz, "version", None) == INGEST_VERSION:
            return biz
    biz = build_business(split_dir, biz_id)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with cache.open("wb") as f:
            pickle.dump(biz, f, protocol=pickle.HIGHEST_PROTOCOL)
    return biz


def build_business(split_dir: Path, biz_id: str) -> Business:
    t0 = time.time()
    bdir = Path(split_dir) / "data" / biz_id
    readme_path = bdir / "README.md"
    readme = parse_readme(readme_path.read_text(encoding="utf-8") if readme_path.exists() else "")
    known = readme.known_names()
    warn: list[str] = []

    # 1) raw sheets -> tables with headers
    pieces: list[tuple[RawSheet, pd.DataFrame]] = []
    for path in sorted(bdir.iterdir()):
        if path.suffix.lower() not in DATA_SUFFIXES:
            continue
        try:
            sheets = read_file(path)
        except Exception as e:  # keep going: one broken file must not kill the business
            warn.append(f"cannot read {path.name}: {e!r}")
            continue
        for sh in sheets:
            df = extract_table(sh, known)
            if df is None or df.empty:
                if not sh.df.empty:
                    warn.append(f"no table found in {sh.file}[{sh.sheet}]")
                continue
            pieces.append((sh, df))

    # 2) union pieces with identical column sets
    groups: dict[tuple, list[tuple[RawSheet, pd.DataFrame]]] = {}
    for sh, df in pieces:
        key = ("sqlite", sh.file, sh.sheet) if sh.has_header else ("cols", tuple(sorted(df.columns)))
        groups.setdefault(key, []).append((sh, df))

    tables: dict[str, pd.DataFrame] = {}
    info: dict[str, TableInfo] = {}
    multi_schema_files = _files_with_multiple_schemas(groups)
    for members in groups.values():
        name = _unique(_table_name(members, multi_schema_files), tables)
        cols = list(members[0][1].columns)
        frames = []
        for sh, df in members:
            df = df[cols].copy()
            df["_file"], df["_sheet"] = sh.file, sh.sheet
            frames.append(df)
        df = pd.concat(frames, ignore_index=True)
        descs: dict[str, str] = {}
        for sh, _ in members:
            for k, v in readme.descs_for(sh.file, sh.sheet).items():
                descs.setdefault(k, v)
        sources = [(sh.file, sh.sheet) for sh, _ in members]
        ti = TableInfo(name=name, sources=sources, descs=descs, roles=assign_roles(cols, descs))
        df = _clean(df, ti)
        tables[name] = df
        info[name] = ti

    return Business(biz_id, Path(split_dir).name, readme, tables, info, warn, time.time() - t0)


def _files_with_multiple_schemas(groups) -> set[str]:
    seen: dict[str, set] = {}
    for key, members in groups.items():
        for sh, _ in members:
            seen.setdefault(sh.file, set()).add(key)
    return {f for f, keys in seen.items() if len(keys) > 1}


_SPLIT_SUFFIX = re.compile(r"(_(\d+|T\d{1,2}_\d{4}|Thang\d{1,2}_\d{4}|\d{4}))+$", re.I)


def _table_name(members, multi_schema_files: set[str]) -> str:
    sh0 = members[0][0]
    if sh0.has_header:
        return f"{Path(sh0.file).stem}.{sh0.sheet}"
    stems = {Path(sh.file).stem for sh, _ in members}
    base = Path(sh0.file).stem if len(stems) == 1 else _SPLIT_SUFFIX.sub("", sorted(stems)[0])
    sheets = {sh.sheet for sh, _ in members}
    generic = all(s is None or _GENERIC_SHEET.match(s.strip()) for s in sheets)
    if not generic and (sh0.file in multi_schema_files or len(sheets) == 1):
        return f"{base}.{sh0.sheet}"
    return base


def _unique(name: str, existing) -> str:
    out, i = name, 2
    while out in existing:
        out, i = f"{name}#{i}", i + 1
    return out


def _clean(df: pd.DataFrame, ti: TableInfo) -> pd.DataFrame:
    data_cols = [c for c in df.columns if c not in META_COLS]
    id_col = next((c for c, r in ti.roles.items() if r == "order_id"), None)
    filled: list[str] = []
    if id_col:
        filled = _forward_fill(df, id_col, data_cols)
    df = _type_columns(df, data_cols, ti)
    if id_col:
        filled += _group_fill(df, id_col, data_cols, skip=set(filled))
        if filled:
            ti.notes.append(f"forward-filled order-level columns on continuation rows: {', '.join(filled)}")
        n = len(df)
        df = df.drop_duplicates(subset=data_cols).reset_index(drop=True)
        if len(df) < n:
            ti.notes.append(f"removed {n - len(df)} exact-duplicate rows within the same `{id_col}` (rulebook §8)")
    for c, r in ti.roles.items():
        if r == "status" and c in df.columns:
            df[f"{c}__norm"] = df[c].map(lambda v: None if _is_null(v) else normalize_status(v))
            ti.notes.append(f"`{c}__norm` = `{c}` mapped to hoàn thành/huỷ/trả hàng/đang giao")
    return df


def _forward_fill(df: pd.DataFrame, id_col: str, data_cols: list[str]) -> list[str]:
    """Rulebook §8: blank cells on continuation rows equal the first row of the group (in place)."""
    blank_id = df[id_col].isna()
    filled: list[str] = []
    if 0.02 < blank_id.mean() < 0.98 and not blank_id.iloc[0]:
        for c in data_cols:
            if df.loc[blank_id, c].isna().mean() > 0.95 and df.loc[~blank_id, c].notna().mean() > 0.5:
                df[c] = df[c].where(~blank_id, df[c].ffill())
                filled.append(c)
    return filled


def _group_fill(df: pd.DataFrame, id_col: str, data_cols: list[str], skip: set[str]) -> list[str]:
    """Order-level columns left blank on some rows of an order get the order's value (in place, after typing)."""
    filled: list[str] = []
    grp = df.groupby(id_col, sort=False)
    for c in data_cols:
        if c == id_col or c in skip or not df[c].isna().any():
            continue
        nn = grp[c].count()
        multi = nn[nn >= 2]
        if len(multi) < 30:
            continue
        nun = grp[c].nunique()
        if (nun.loc[multi.index] <= 1).mean() >= 0.99:
            before = df[c].isna().sum()
            df[c] = df[c].fillna(grp[c].transform("first"))
            if df[c].isna().sum() < before:
                filled.append(c)
    return filled


def _type_columns(df: pd.DataFrame, data_cols: list[str], ti: TableInfo) -> pd.DataFrame:
    years = [year_month_hint(str(s or ""), str(f))[0] for f, s in zip(df["_file"], df["_sheet"])]
    for c in data_cols:
        col = df[c]
        nn = col.dropna()
        if nn.empty:
            continue
        sample = nn.sample(min(len(nn), 2000), random_state=0) if len(nn) > 2000 else nn
        h = fold(c)
        role = ti.roles.get(c)
        desc = fold(ti.descs.get(c, ""))
        name_says_date = bool(_DATE_NAME.search(h)) or role in ("order_date", "order_ts") or desc.startswith("ngay")
        explicit = sample.map(is_explicit_date).mean()
        allow_serial = name_says_date or explicit >= 0.3
        date_rate = sample.map(lambda v: parse_date(v, 2000, allow_serial) is not None).mean()
        if (name_says_date and date_rate >= 0.8) or (explicit >= 0.6 and date_rate >= 0.8):
            df[c] = pd.to_datetime(
                [parse_date(v, y, allow_serial) for v, y in zip(col, years)], errors="coerce"
            )
            continue
        if _CODE_NAME.search(h) or role in ("order_id", "product_id", "customer_id"):
            df[c] = col.map(lambda v: None if _is_null(v) else _as_code(v))
            continue
        num_rate = sample.map(lambda v: parse_number(v) is not None).mean()
        if num_rate >= 0.9:
            if sample.map(lambda v: isinstance(v, str) and parse_unit(v) is not None).any():
                df[f"{c}__unit"] = col.map(parse_unit)
                ti.notes.append(f"`{c}` had units inside the cell; number kept in `{c}`, unit in `{c}__unit`")
            vals = pd.to_numeric(pd.Series([parse_number(v) for v in col], index=df.index), errors="coerce")
            if vals.notna().all() and np.allclose(vals, vals.round()) and vals.abs().max() < 2**53:
                vals = vals.round().astype("int64")
            df[c] = vals
            continue
        df[c] = col.map(lambda v: None if _is_null(v) else str(v).strip())
    return df


def _as_code(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()
