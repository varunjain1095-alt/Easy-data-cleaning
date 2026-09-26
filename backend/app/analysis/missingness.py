"""Missingness workflow assessment (architecture section 3).

Recognizes missing values, reports per-column detail, lists the treatments
available for the inferred intended type, and attaches an advisory
recommendation. Nothing is applied here.
"""

import polars as pl

from ..config import ROW_ID_COLUMN
from ..masking import (
    is_datetime_dtype,
    is_numeric_dtype,
    marker_breakdown,
    markers_param,
    missing_expr,
)
from .typing import infer_column

# availability by intended type (architecture 3.3)
METHODS_BY_TYPE = {
    "integer": ["constant", "mean", "median", "mode", "interpolate_linear", "interpolate_time", "group_impute", "random_sample"],
    "decimal": ["constant", "mean", "median", "mode", "interpolate_linear", "interpolate_time", "group_impute", "random_sample"],
    "categorical": ["mode", "constant", "group_impute", "random_sample"],
    "string": ["constant"],
    "text": ["constant"],
    "date": ["forward_fill", "backward_fill", "constant", "interpolate_time"],
    "datetime": ["forward_fill", "backward_fill", "constant", "interpolate_time"],
    "time": ["forward_fill", "backward_fill", "constant"],
    "boolean": ["mode", "constant", "group_impute"],
}
UNIVERSAL = ["leave", "drop_rows", "drop_column"]


def _skewness(df: pl.DataFrame, col: str) -> float | None:
    try:
        s = df[col].drop_nulls()
        if df.schema[col] == pl.String:
            s = s.str.strip_chars().cast(pl.Float64, strict=False).drop_nulls()
        if s.len() < 3:
            return None
        return float(s.skew())
    except Exception:
        return None


def _recommend(df: pl.DataFrame, col: str, suggested: str, missing_pct: float) -> dict | None:
    if missing_pct == 0:
        return None
    if missing_pct >= 50:
        return {
            "method": "drop_column",
            "reason": f"{missing_pct:.0f}% of values are missing; keeping the column adds little information.",
        }
    if suggested in ("integer", "decimal"):
        skew = _skewness(df, col)
        if skew is not None and abs(skew) >= 1.0:
            return {
                "method": "median",
                "reason": f"Distribution is strongly skewed (|skew| = {abs(skew):.2f} ≥ 1.0); mean imputation would be pulled by outliers.",
            }
        return {"method": "mean", "reason": "Numeric column with roughly symmetric distribution."}
    if suggested in ("categorical", "boolean"):
        return {"method": "mode", "reason": "Most frequent observed value is a safe default for categorical/boolean data."}
    if suggested in ("date", "datetime", "time"):
        return {
            "method": "forward_fill",
            "reason": "Forward fill preserves temporal order; confirm the ordering column before applying.",
        }
    if suggested in ("string", "text"):
        return {"method": "constant", "reason": "A constant placeholder preserves the column without inventing data."}
    return None


def assess_missingness(df: pl.DataFrame, params: dict | None = None) -> dict:
    markers = markers_param(params)
    columns = [c for c in df.columns if c != ROW_ID_COLUMN]
    n_rows = df.height
    out = []
    for col in columns:
        mask = missing_expr(col, markers)
        count = int(df.select(mask.sum()).item())
        pct = (count / n_rows * 100) if n_rows else 0.0
        inferred = infer_column(df, col, markers)
        suggested = inferred["suggested"]
        examples = (
            df.select([ROW_ID_COLUMN, col])
            .filter(mask)
            .head(5)
            .to_dicts()
        )
        methods = UNIVERSAL + METHODS_BY_TYPE.get(suggested, ["constant"])
        out.append(
            {
                "column": col,
                "missing_count": count,
                "missing_pct": round(pct, 2),
                "marker_breakdown": marker_breakdown(df, col),
                "suggested_type": suggested,
                "current_dtype": str(df.schema[col]),
                "example_rows": examples,
                "available_methods": methods,
                "recommendation": _recommend(df, col, suggested, pct),
            }
        )
    return {"row_count": n_rows, "markers": markers, "columns": out}


def unimputable_groups(df: pl.DataFrame, col: str, group_by: list[str], markers: list[str]) -> dict:
    """Groups with no usable observed value for group-based imputation (3.4)."""
    from ..masking import normalize_missing

    df2 = normalize_missing(df, col, markers)
    stats = (
        df2.group_by(group_by)
        .agg(
            pl.col(col).drop_nulls().len().alias("observed"),
            pl.len().alias("rows"),
        )
        .filter(pl.col("observed") == 0)
    )
    return {
        "empty_group_count": stats.height,
        "empty_groups": stats.select(group_by).to_dicts()[:50],
        "min_usable": 5,  # recommendation default (section 14)
    }
