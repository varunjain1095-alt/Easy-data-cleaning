"""Pattern & structure validation (post-invalid-values stage).

A column may have the right dtype but inconsistent internal structure —
emails, phones, postcodes, product IDs. The user defines or selects an
expected pattern; the assessment reports non-matching values as proof.
Treatments are applied via the `clean_pattern` op.
"""

import re

import polars as pl

from ..config import ROW_ID_COLUMN
from ..masking import missing_expr

PATTERN_PRESETS = {
    "email": r"^[\w.+-]+@[\w-]+\.[\w.-]+$",
    "phone_intl": r"^\+?\d[\d\s().-]{6,}$",
    "postcode_us": r"^\d{5}(-\d{4})?$",
    "postcode_uk": r"^[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}$",
    "digits_only": r"^\d+$",
    "alphanumeric_id": r"^[A-Za-z0-9_-]+$",
}

# Guardrails for custom regexes: Rust regex (Polars) is linear-time, but we
# still reject overlong patterns and constructs Rust's engine won't compile.
MAX_PATTERN_LEN = 300


def validate_pattern(pattern: str) -> str | None:
    """Return an error string if the pattern is unusable, else None."""
    if not pattern:
        return "empty pattern"
    if len(pattern) > MAX_PATTERN_LEN:
        return f"pattern too long ({len(pattern)} > {MAX_PATTERN_LEN})"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"invalid regex: {exc}"
    if re.search(r"\(\?[=!<]|\(\?P|\\[1-9]", pattern):
        return "lookahead/lookbehind/backreferences are not supported"
    return None


def compile_pattern(name_or_regex: str) -> tuple[str, str] | tuple[None, str]:
    """Resolve a preset name or custom regex -> (pattern, None) | (None, error)."""
    pattern = PATTERN_PRESETS.get(name_or_regex, name_or_regex)
    err = validate_pattern(pattern)
    return (None, err) if err else (pattern, None)


def assess_pattern(df: pl.DataFrame, column: str, name_or_regex: str, markers: list[str]) -> dict:
    """Report how many non-missing values fail the expected pattern."""
    if column not in df.columns:
        return {"error": f"Unknown column: {column}"}
    if df.schema[column] != pl.String:
        return {"error": f"Pattern checks apply to text columns — {column} is {df.schema[column]}"}
    pattern, err = compile_pattern(name_or_regex)
    if err:
        return {"error": err}

    valid = df.filter(~missing_expr(column, markers))
    total = valid.height
    if total == 0:
        return {"column": column, "pattern": pattern, "total": 0,
                "matching": 0, "non_matching": 0, "pct_matching": None, "samples": []}
    matches = valid[column].str.contains(f"^{pattern}$" if not pattern.startswith("^") else pattern)
    non = valid.filter(~pl.Series(matches))
    return {
        "column": column,
        "pattern": pattern,
        "total": total,
        "matching": total - non.height,
        "non_matching": non.height,
        "pct_matching": round(100 * (total - non.height) / total, 2),
        "samples": non[column].head(10).to_list(),
    }
