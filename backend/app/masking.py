"""Recognized missing-value handling.

Recognized missing = actual nulls, blank/whitespace strings, and common
markers (NA, N/A, null, none, nan, -, --). Used consistently by missingness
assessment, type inference, and duplicate comparison.
"""

import polars as pl

from .config import MISSING_MARKERS


def markers_param(params: dict | None) -> list[str]:
    if params and params.get("markers"):
        return list(params["markers"])
    return sorted(MISSING_MARKERS)


def missing_expr(col: str, markers: list[str]) -> pl.Expr:
    """Boolean expr: cell is a recognized missing value."""
    return (
        pl.col(col).is_null()
        | (
            pl.col(col).cast(pl.String).str.strip_chars().str.to_lowercase().is_in(markers)
            & pl.col(col).cast(pl.String).is_not_null()
        )
    )


def normalize_missing(df: pl.DataFrame, col: str, markers: list[str]) -> pl.DataFrame:
    """Replace recognized missing markers in a string column with real nulls."""
    if col not in df.columns or df.schema[col] != pl.String:
        return df
    mask = pl.col(col).str.strip_chars().str.to_lowercase().is_in(markers)
    return df.with_columns(
        pl.when(mask).then(pl.lit(None).cast(pl.String)).otherwise(pl.col(col)).alias(col)
    )


def missing_count(df: pl.DataFrame, col: str, markers: list[str]) -> int:
    return int(df.select(missing_expr(col, markers).sum()).item())


def marker_breakdown(df: pl.DataFrame, col: str) -> dict[str, int]:
    """Counts per missing representation: null, blank/whitespace, per-marker."""
    out: dict[str, int] = {"null": 0}
    if col not in df.columns:
        return out
    out["null"] = int(df[col].null_count())
    if df.schema[col] != pl.String:
        return out
    trimmed = df[col].str.strip_chars().str.to_lowercase()
    vc = trimmed.value_counts().to_dicts()
    for row in vc:
        val = row[col]
        if val is not None and val in MISSING_MARKERS:
            label = "blank" if val == "" else val
            out[label] = out.get(label, 0) + int(row["count"])
    return out


def is_numeric_dtype(dt: pl.DataType) -> bool:
    return dt in (
        pl.Int8, pl.Int16, pl.Int32, pl.Int64,
        pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
        pl.Float32, pl.Float64,
    )


def is_datetime_dtype(dt: pl.DataType) -> bool:
    return dt in (pl.Date, pl.Datetime, pl.Time)
