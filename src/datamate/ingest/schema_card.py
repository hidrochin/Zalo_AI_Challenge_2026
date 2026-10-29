"""Compact text description of a business's cleaned tables, for the LLM prompt."""

import pandas as pd

from datamate.ingest.business import META_COLS, Business


def _fmt(v) -> str:
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).replace("\n", " ").replace("|", "/")
    return s if len(s) <= 40 else s[:37] + "..."


def _column_line(df: pd.DataFrame, c: str, role: str | None, desc: str | None) -> str:
    col = df[c]
    nn = col.dropna()
    null_pct = 100 * (1 - len(nn) / max(1, len(col)))
    if pd.api.types.is_datetime64_any_dtype(col):
        kind = "datetime"
        ex = f"{_fmt(nn.min())} .. {_fmt(nn.max())}" if len(nn) else "-"
    elif pd.api.types.is_numeric_dtype(col):
        kind = "int" if pd.api.types.is_integer_dtype(col) else "float"
        ex = f"{_fmt(nn.min())} .. {_fmt(nn.max())}" if len(nn) else "-"
    else:
        kind = "str"
        vc = nn.value_counts()
        if 0 < len(vc) <= 12:
            ex = "{" + ", ".join(f"{_fmt(k)}:{n}" for k, n in vc.items()) + "}"
        else:
            ex = ", ".join(_fmt(v) for v in vc.index[:3]) + (f" … ({len(vc)} distinct)" if len(vc) > 3 else "")
    parts = [f"`{c}`", kind]
    if role:
        parts.append(f"role={role}")
    if null_pct >= 1:
        parts.append(f"null={null_pct:.0f}%")
    line = "- " + " | ".join(parts) + f" | {ex}"
    if desc:
        line += f" — README: {desc}"
    return line


def schema_card(biz: Business) -> str:
    out = [f"# Schema card: {biz.id}"]
    if biz.readme.data_end is not None:
        out.append(f"Data recorded until {biz.readme.data_end:%Y-%m-%d} (from README).")
    out.append(
        "Tables are preloaded as pandas DataFrames in the dict `T`. Cleaning already done: header/title rows "
        "removed, monthly sheets and split files concatenated (`_file`, `_sheet` say where a row came from), "
        "money/quantity text parsed to numbers, mixed date formats parsed to datetime."
    )
    for name, df in biz.tables.items():
        ti = biz.info[name]
        files = sorted({f for f, _ in ti.sources})
        nsheets = len({s for _, s in ti.sources})
        src = ", ".join(files) + (f" ({nsheets} sheets)" if nsheets > 1 else "")
        out.append(f"\n## T[{name!r}]  rows={len(df)}  source: {src}")
        for c in df.columns:
            if c in META_COLS:
                continue
            out.append(_column_line(df, c, ti.roles.get(c), ti.descs.get(c)))
        for n in ti.notes:
            out.append(f"  note: {n}")
    if biz.warnings:
        out.append("\nIngestion warnings: " + "; ".join(biz.warnings))
    return "\n".join(out)
