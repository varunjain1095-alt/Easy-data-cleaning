"""Data profile: read-only per-column audit shown before cleaning begins.

Severity mirrors the treatment stages: missingness bands plus a type-
inconsistency bump when numeric-looking values are stored as text.
"""

import polars as pl

from ..config import ROW_ID_COLUMN
from ..masking import markers_param, missing_expr

_SAMPLE = 300

_SEVERITY_ORDER = {"none": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}


def _missing_severity(pct: float) -> str:
    if pct <= 0:
        return "none"
    if pct < 25:
        return "low"
    if pct < 50:
        return "moderate"
    if pct < 75:
        return "high"
    return "critical"


def _suspect_numeric(df: pl.DataFrame, col: str, mask: pl.Expr) -> bool:
    """True when a text column's observed values are >90% numeric-parseable."""
    if df.schema[col] != pl.String:
        return False
    sample = (
        df.select(col)
        .filter(~mask)
        .head(_SAMPLE)
        .get_column(col)
    )
    if sample.len() == 0:
        return False
    numeric = sample.cast(pl.Float64, strict=False)
    return float(numeric.is_not_null().mean()) > 0.9


def assess_profile(df: pl.DataFrame, params: dict | None = None) -> dict:
    markers = markers_param(params)
    n_rows = df.height
    out = []
    for col in [c for c in df.columns if c != ROW_ID_COLUMN]:
        mask = missing_expr(col, markers)
        missing = int(df.select(mask.sum()).item())
        missing_pct = round(missing / n_rows * 100, 2) if n_rows else 0.0
        unique = int(df.filter(~mask).select(col).n_unique()) if n_rows else 0
        repeated = n_rows - unique if n_rows else 0
        repeated_pct = round(repeated / n_rows * 100, 2) if n_rows else 0.0
        suspect_numeric = _suspect_numeric(df, col, mask)

        severity = _missing_severity(missing_pct)
        reasons = []
        if missing:
            reasons.append(f"{missing_pct}% missing")
        if suspect_numeric:
            reasons.append("numeric-looking values stored as text")
            if _SEVERITY_ORDER[severity] < _SEVERITY_ORDER["moderate"]:
                severity = "moderate"

        out.append(
            {
                "column": col,
                "missing_count": missing,
                "missing_pct": missing_pct,
                "dtype": str(df.schema[col]),
                "suspect_numeric_object": suspect_numeric,
                "unique_count": unique,
                "repeated_count": repeated,
                "repeated_pct": repeated_pct,
                "is_unique_key": n_rows > 0 and unique == n_rows,
                "has_repeats": unique < n_rows,
                "severity": severity,
                "reasons": reasons,
            }
        )
    out.sort(
        key=lambda c: (-_SEVERITY_ORDER[c["severity"]], -c["missing_pct"], c["column"])
    )
    return {"row_count": n_rows, "columns": out}
