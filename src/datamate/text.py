"""Text normalization shared by ingestion, retrieval, helpers and the scorer."""

import unicodedata


def fold(s) -> str:
    """Lowercase, strip Vietnamese diacritics (đ -> d), collapse whitespace."""
    s = unicodedata.normalize("NFD", str(s).lower()).replace("đ", "d")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def norm_key(s) -> str:
    """The official string-matching normalization (diacritics kept).

    NFC -> lowercase -> every char that is not a letter/digit/whitespace becomes a space
    -> trim and collapse whitespace.
    """
    s = unicodedata.normalize("NFC", str(s)).lower()
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s)
    return " ".join(s.split())
