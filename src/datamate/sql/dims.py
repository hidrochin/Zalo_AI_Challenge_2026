"""Entity keys and dimensions: every spelling of a product or customer resolves to one key.

Rulebook: a shop with codes identifies entities by code (two codes with one name are two entities); without codes,
two names that differ only in how they are written are one entity, and names with different letters are two. So
'Bia  Sài Gòn 330 ml' = 'BIA SAI GON 330ML', but 'Doãn Dũng Thanh' != 'Đoàn Dũng Thanh' though both fold to the same
text: a word may differ only where one side has no diacritics. There is no fuzzy matching here.
"""

import re

import pandas as pd

from datamate.text import fold, norm_key

_UNIT_GAP = re.compile(r"(\d) (ml|l|g|kg|mg|gb|tb|cm|mm)\b")


def name_form(v) -> str:
    """Name without case, punctuation and spacing differences; diacritics kept: 'Coca  Cola 330 ml' -> 'coca cola 330ml'."""
    return _UNIT_GAP.sub(r"\1\2", norm_key(v))


def same_spelling(a: str, b: str) -> bool:
    """True if two name forms are one name: each differing word has no diacritics on one side."""
    ta, tb = a.split(), b.split()
    return len(ta) == len(tb) and all(x == y or x == fold(x) or y == fold(y) for x, y in zip(ta, tb))


def _marked_words(form: str) -> int:
    return sum(w != fold(w) for w in form.split())


def name_keys(names: pd.Series) -> pd.Series:
    """Key of each name: the fullest form it is a spelling of. Missing names stay missing.

    Forms with the most diacritics found the clusters: same_spelling is not transitive, so a bare 'doan thanh' must not
    bridge 'doãn thanh' and 'đoàn thanh'. A bare form that fits several clusters joins the most frequent one."""
    known = names.dropna()
    form_of = {v: name_form(v) for v in known.unique()}
    forms = known.map(form_of)
    counts = forms.value_counts()
    keys: dict[str, str] = {}
    heads: dict[str, list[str]] = {}  # folded form -> cluster representatives
    for form in sorted(counts.index, key=lambda f: (-_marked_words(f), -counts[f], f)):
        reps = heads.setdefault(fold(form), [])
        rep = next((r for r in reps if same_spelling(r, form)), None)
        if rep is None:
            reps.append(form)
        keys[form] = rep or form
    return forms.map(keys).reindex(names.index)


def _clean_code(v) -> str:
    return str(v).strip()


def entity_keys(code: pd.Series | None, name: pd.Series | None) -> pd.Series:
    """Code if the row has one; else the code its name uniquely belongs to; else the name key."""
    key = name_keys(name) if name is not None else None
    if code is None:
        return key
    c = code.map(_clean_code, na_action="ignore")
    if key is None:
        return c
    pairs = pd.DataFrame({"k": key, "c": c}).dropna().drop_duplicates()
    single = pairs.groupby("k")["c"].transform("nunique") == 1
    named = c.where(c.notna(), key.map(pairs[single].set_index("k")["c"]))
    return named.where(named.notna(), key)


def build_dim(key: pd.Series, code: pd.Series | None, name: pd.Series) -> pd.DataFrame:
    """One row per entity: entity_key, code, display_name (most frequent spelling), aliases (all spellings), n_lines."""
    df = pd.DataFrame({"entity_key": key, "name": name.astype("object")}).dropna(subset=["entity_key"])
    df["code"] = code.map(_clean_code, na_action="ignore") if code is not None else None
    rows = []
    for k, g in df.groupby("entity_key", sort=True):
        spellings = g["name"].dropna().astype(str).value_counts()
        ordered = sorted(spellings.index, key=lambda s: (-spellings[s], s))
        codes = g["code"].dropna()
        rows.append({"entity_key": k, "code": codes.iloc[0] if len(codes) else None,
                     "display_name": ordered[0] if ordered else None, "aliases": ordered, "n_lines": len(g)})
    return pd.DataFrame(rows, columns=["entity_key", "code", "display_name", "aliases", "n_lines"])


def _unify(out: pd.DataFrame, entity: str) -> None:
    code, name = out.get(f"{entity}_id"), out.get(f"{entity}_name")
    if code is None and name is None:
        return
    key = entity_keys(code, name)
    out[f"{entity}_key"] = key.where(~out["walkin"]) if entity == "customer" else key
    if name is not None:
        key = out[f"{entity}_key"]
        out[f"{entity}_name_raw"] = name
        display = key.map(build_dim(key, code, name).set_index("entity_key")["display_name"])
        out[f"{entity}_name"] = display.where(key.notna(), name)


def with_entity_keys(sales: pd.DataFrame) -> pd.DataFrame:
    """Add product_key / customer_key and leave one spelling per entity in product_name / customer_name, so a filter or
    group-by on the name sees the whole entity. Source spellings stay in *_name_raw. Walk-ins have no customer key."""
    out = sales.copy()
    _unify(out, "product")
    _unify(out, "customer")
    return out


def entity_dims(sales: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """{'dim_product': ..., 'dim_customer': ...} of sales built by with_entity_keys."""
    dims = {}
    for entity in ("product", "customer"):
        if f"{entity}_key" in sales.columns:
            code = sales.get(f"{entity}_id")
            dims[f"dim_{entity}"] = build_dim(sales[f"{entity}_key"], code, sales.get(f"{entity}_name_raw", code))
    return dims
