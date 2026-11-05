"""Read raw files into sheets, find the header row, and cut out the data table."""

import csv
import datetime as dt
import re
import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from datamate.ingest.parsers import _is_null, is_explicit_date, parse_date, parse_number
from datamate.text import fold

DATA_SUFFIXES = {".csv", ".xlsx", ".xls", ".sqlite", ".db"}


@dataclass
class RawSheet:
    file: str
    sheet: str | None
    df: pd.DataFrame
    has_header: bool  # sqlite tables already have proper column names


def read_file(path: Path) -> list[RawSheet]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sheets = pd.read_excel(path, sheet_name=None, header=None, engine="calamine", dtype=object)
        return [RawSheet(path.name, name, df, False) for name, df in sheets.items()]
    if suffix == ".csv":
        return [RawSheet(path.name, None, _read_csv(path), False)]
    if suffix in (".sqlite", ".db"):
        out = []
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
            for t in names:
                out.append(RawSheet(path.name, t, pd.read_sql_query(f'select * from "{t}"', con), True))
        finally:
            con.close()
        return out
    return []


def _read_csv(path: Path) -> pd.DataFrame:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp1258", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        sep = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        sep = ","
    from io import StringIO

    return pd.read_csv(StringIO(text), header=None, dtype=str, sep=sep, keep_default_na=False, na_values=[""])


def _is_label(v) -> bool:
    return isinstance(v, str) and bool(v.strip()) and parse_number(v) is None and parse_date(v, 2000, False) is None


def detect_header(raw: pd.DataFrame, known: set[str], scan: int = 15) -> int | None:
    """Index of the most header-like row among the first `scan` rows."""
    width = max(1, int(raw.notna().sum().gt(0).sum()))
    best, best_score = None, 0.0
    for i in range(min(scan, len(raw))):
        cells = [v for v in raw.iloc[i].tolist() if not _is_null(v)]
        if len(cells) < 2:
            continue
        labels = [v for v in cells if _is_label(v)]
        if len(labels) / width < 0.5:
            continue
        matches = sum(fold(v) in known for v in labels)
        score = len(labels) + 3 * matches - 2 * (len(cells) - len(labels))
        if score > best_score:
            best, best_score = i, score
    return best


# Folded, the surname "Tống" and products like "Công tắc …" also start with tong / cong: a bare prefix is not a total.
_TOTAL = re.compile(r"^(tong cong|total)\b|^(tong|cong)$")
# subtotal / note rows inside the data: "Tổng ngày 01/03", "Cộng ngày 01/05/2025", "TỔNG THÁNG", "tổng 01/05", "Ghi chú: …"
_SUBTOTAL = re.compile(r"^(tong|cong) (ngay|thang|tuan|ca)( \d|$)|^(tong|cong) \d{1,2}[/-]\d{1,2}|^ghi chu ?:")


def _subtotal_rows(df: pd.DataFrame) -> pd.Series:
    """Rows with a subtotal/note label and no date anywhere (a sale by customer "Tống Thắng" still has its date)."""
    cand = pd.Series(False, index=df.index)
    for c in df.columns:
        u = df[c].dropna().unique()
        hits = {v for v in u if isinstance(v, str) and _SUBTOTAL.match(fold(v))}
        if hits:
            cand |= df[c].isin(hits)
    if not cand.any():
        return cand
    dated = df[cand].apply(lambda r: any(isinstance(v, (dt.date, dt.datetime)) or (isinstance(v, str) and is_explicit_date(v))
                                         for v in r), axis=1)
    cand[cand] = ~dated
    return cand


def extract_table(sheet: RawSheet, known: set[str]) -> pd.DataFrame | None:
    """Turn a raw sheet into a table with string column names (values left raw)."""
    raw = sheet.df
    if sheet.has_header:
        df = raw.copy()
        df.columns = [str(c) for c in df.columns]
        return df.astype(object).map(lambda v: None if _is_null(v) else v)
    if raw.empty:
        return None
    h = detect_header(raw, known)
    if h is None:
        return None
    header = ["" if _is_null(v) else str(v).strip() for v in raw.iloc[h].tolist()]
    body = raw.iloc[h + 1:].astype(object)
    body = body.map(lambda v: None if _is_null(v) else (v.strip() if isinstance(v, str) else v))
    keep, names, seen = [], [], {}
    for j, name in enumerate(header):
        col = body.iloc[:, j]
        if not name and col.isna().all():
            continue
        name = name or f"col{j}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        keep.append(j)
        names.append(name)
    df = body.iloc[:, keep].copy()
    df.columns = names
    df = df.dropna(how="all")
    # repeated header rows (page breaks) and trailing total rows
    hdr = [fold(n) for n in names]
    is_hdr = df.apply(lambda r: sum(fold(v) == hdr[k] for k, v in enumerate(r) if v is not None) >= max(2, len(names) // 2), axis=1)
    first = df.apply(lambda r: next((v for v in r if v is not None), None), axis=1)
    sparse = df.notna().sum(axis=1) <= max(1, len(names) // 2)
    is_total = first.map(lambda v: isinstance(v, str) and bool(_TOTAL.match(fold(v)))) & sparse
    df = df[~(is_hdr | is_total | _subtotal_rows(df))]
    return df.reset_index(drop=True)
