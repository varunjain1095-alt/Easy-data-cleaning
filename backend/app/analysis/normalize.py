"""Value-normalization assessment (architecture section 5).

Safe operations (whitespace, line breaks) may be strongly recommended.
Meaning-changing operations (case, punctuation, label mapping) always require
explicit user selection and confirmation.
"""

import polars as pl

from ..config import ROW_ID_COLUMN

SAFE_OPS = ["trim", "collapse_spaces", "strip_linebreaks"]
MEANING_CHANGING_OPS = ["lower", "upper", "title", "standardize_punctuation", "map_values"]


def _count(df: pl.DataFrame, col: str, expr: pl.Expr) -> int:
    return int(df.select(expr.sum().fill_null(0)).item())


def assess_column(df: pl.DataFrame, col: str) -> dict:
    s = pl.col(col)
    safe = {
        "trim": _count(df, col, s.str.strip_chars() != s),
        "collapse_spaces": _count(df, col, s.str.contains(r" {2,}")),
        "strip_linebreaks": _count(df, col, s.str.contains(r"[\r\n\t]")),
        "empty_to_null": _count(df, col, s.str.strip_chars() == ""),
    }
    meaning = {
        "lower": _count(df, col, s.str.to_lowercase() != s),
        "upper": _count(df, col, s.str.to_uppercase() != s),
        "title": _count(df, col, s.str.to_titlecase() != s),
        "standardize_punctuation": _count(
            df, col, s.str.contains(r"[’‘`´“”\"—–…]")
        ),
    }

    # Candidate categorical label groups: same normalized form, >1 distinct labels.
    labels = (
        df.select(
            s.str.strip_chars()
            .str.replace_all(r" {2,}", " ")
            .str.to_lowercase()
            .alias("__key"),
            s.alias("__orig"),
        )
        .filter(pl.col("__orig").is_not_null())
        .unique()
        .group_by("__key")
        .agg(pl.col("__orig").unique().alias("variants"), pl.len().alias("n"))
        .filter(pl.col("n") > 1)
        .to_dicts()
    )
    suggested_mappings = [
        {
            "canonical": max(g["variants"], key=len),
            "variants": sorted(g["variants"]),
        }
        for g in labels
    ]

    return {
        "column": col,
        "safe": safe,
        "meaning_changing": meaning,
        "suggested_mappings": suggested_mappings[:50],
    }


def assess_normalization(df: pl.DataFrame) -> dict:
    cols = [
        c
        for c in df.columns
        if c != ROW_ID_COLUMN and df.schema[c] == pl.String
    ]
    return {
        "safe_operations": SAFE_OPS,
        "meaning_changing_operations": MEANING_CHANGING_OPS,
        "columns": [assess_column(df, c) for c in cols],
    }
