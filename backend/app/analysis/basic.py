"""Remaining basic cleaning assessment (architecture section 7).

Column-name cleaning, invalid-value validation, empty/constant structures,
and basic format-standardization candidates. Nothing mutates here.
"""

import re
from datetime import date, datetime

import polars as pl

from ..config import ROW_ID_COLUMN
from ..masking import is_numeric_dtype, marker_breakdown

_NAME_BAD = re.compile(r"[^a-z0-9_]")


def propose_column_name(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"[^a-z0-9]+", "_", n).strip("_")
    return n or "column"


def column_name_assessment(df: pl.DataFrame) -> dict:
    issues = []
    proposed: dict[str, str] = {}
    for c in df.columns:
        if c == ROW_ID_COLUMN:
            continue
        new = propose_column_name(c)
        problems = []
        if c != c.strip():
            problems.append("surrounding_whitespace")
        if _NAME_BAD.search(c.lower().replace(" ", "_")) or " " in c:
            problems.append("unsupported_characters")
        if c != c.lower():
            problems.append("case")
        if new != c or problems:
            issues.append({"column": c, "proposed": new, "problems": problems})
            proposed[c] = new
    # collisions after renaming
    seen: dict[str, list[str]] = {}
    for c in df.columns:
        if c == ROW_ID_COLUMN:
            continue
        key = proposed.get(c, c)
        seen.setdefault(key, []).append(c)
    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    return {"issues": issues, "collisions": collisions, "proposed_mapping": proposed}


def structure_assessment(df: pl.DataFrame) -> dict:
    cols = [c for c in df.columns if c != ROW_ID_COLUMN]
    empty_row_mask = pl.all_horizontal([pl.col(c).is_null() for c in cols])
    empty_rows = df.filter(empty_row_mask)[ROW_ID_COLUMN].to_list()
    empty_cols, constant_cols = [], []
    for c in cols:
        non_null = df[c].drop_nulls()
        if non_null.len() == 0:
            empty_cols.append(c)
        elif non_null.n_unique() == 1:
            constant_cols.append({"column": c, "value": non_null[0]})
    return {
        "empty_row_count": len(empty_rows),
        "empty_row_ids": empty_rows,
        "empty_columns": empty_cols,
        "constant_columns": constant_cols,
    }


def _looks_numeric(s: pl.Series) -> float:
    vals = s.drop_nulls().cast(pl.String).str.strip_chars()
    if vals.len() == 0:
        return 0.0
    ok = vals.cast(pl.Float64, strict=False).drop_nulls().len()
    return ok / vals.len()


def format_candidates(df: pl.DataFrame) -> list[dict]:
    """Detect columns that look like they need format standardization."""
    out = []
    for c in df.columns:
        if c == ROW_ID_COLUMN or df.schema[c] != pl.String:
            continue
        s = df[c].drop_nulls()
        if s.len() == 0:
            continue
        sample = s.head(500).to_list()
        lowered = [str(v).strip().lower() for v in sample]
        bool_like = all(v in {"y", "n", "yes", "no", "true", "false", "t", "f", "0", "1"} for v in lowered)
        pct_like = sum(1 for v in sample if str(v).strip().endswith("%")) / len(sample) > 0.8
        cur_like = sum(1 for v in sample if re.match(r"^[$€£₹]?\s?-?[\d,]+(\.\d+)?$", str(v).strip())) / len(sample) > 0.8
        currency_sym = sum(1 for v in sample if re.match(r"^[$€£₹]", str(v).strip())) / len(sample)
        dec_comma = sum(1 for v in sample if re.match(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$", str(v).strip()) and "," in str(v)) / len(sample)
        if bool_like:
            out.append({"column": c, "kind": "boolean", "note": "boolean-like strings"})
        elif pct_like:
            out.append({"column": c, "kind": "percent", "note": "values end with %"})
        elif currency_sym > 0.8:
            out.append({"column": c, "kind": "currency", "note": "currency symbols present"})
        elif cur_like and dec_comma > 0.8:
            out.append({"column": c, "kind": "decimal_separator", "note": "comma decimal separator suspected"})
    return out


def eval_invalid_rule(df: pl.DataFrame, col: str, rule: dict) -> dict:
    """Evaluate an invalid-value rule; returns affected rows for preview/treatment."""
    kind = rule["type"]
    s = df[col]
    if kind == "range":
        num = s.cast(pl.Float64, strict=False)
        mask = pl.lit(False)
        if rule.get("min") is not None:
            mask = mask | (num < rule["min"])
        if rule.get("max") is not None:
            mask = mask | (num > rule["max"])
        mask = mask & num.is_not_null()
    elif kind == "not_in":
        allowed = {str(v) for v in rule["values"]}
        mask = ~s.cast(pl.String).is_in(list(allowed)) & s.is_not_null()
    elif kind == "unparseable_date":
        mask = s.cast(pl.String).str.to_date(rule.get("format"), strict=False).is_null() & s.is_not_null()
    elif kind == "unparseable_numeric":
        mask = s.cast(pl.String).str.strip_chars().cast(pl.Float64, strict=False).is_null() & s.is_not_null()
    else:
        raise ValueError(f"unknown rule {kind}")
    bad = df.filter(mask)
    return {
        "column": col,
        "rule": rule,
        "invalid_count": bad.height,
        "invalid_pct": round(bad.height / df.height * 100, 2) if df.height else 0.0,
        "row_ids": bad[ROW_ID_COLUMN].to_list(),
        "examples": bad.select([ROW_ID_COLUMN, col]).head(10).to_dicts(),
    }


def assess_basic(df: pl.DataFrame) -> dict:
    return {
        "column_names": column_name_assessment(df),
        "structures": structure_assessment(df),
        "format_candidates": format_candidates(df),
    }
