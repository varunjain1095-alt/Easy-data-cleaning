"""Intended data-type inference (architecture section 4).

Inference uses only non-missing values. No suggestion is applied without user
confirmation. Numeric-looking identifiers (ZIP codes, phone numbers, SKUs,
account numbers) remain strings when identifier characteristics are detected.
"""

import re
from datetime import datetime

import polars as pl

from ..masking import is_numeric_dtype, missing_expr

INT_RE = re.compile(r"^-?\d{1,18}$")
FLOAT_RE = re.compile(r"^-?\d{1,15}([.,]\d+)?$")
BOOL_TRUE = {"y", "yes", "true", "t", "1"}
BOOL_FALSE = {"n", "no", "false", "f", "0"}

# Candidate date/datetime formats, each labelled day-first vs month-first.
DATE_FORMATS = [
    ("%Y-%m-%d", None),
    ("%d/%m/%Y", "day_first"),
    ("%m/%d/%Y", "month_first"),
    ("%d-%m-%Y", "day_first"),
    ("%m-%d-%Y", "month_first"),
    ("%d.%m.%Y", "day_first"),
    ("%Y/%m/%d", None),
    ("%d %b %Y", "day_first"),
    ("%B %d, %Y", "month_first"),
]
DATETIME_SUFFIXES = [" %H:%M:%S", " %H:%M", "T%H:%M:%S", "T%H:%M:%S.%f"]
# Time-of-day formats (kind "time") — e.g. 18.00.00, 18:00:00
TIME_FORMATS = [("%H:%M:%S", None), ("%H.%M.%S", None), ("%H:%M", None)]

IDENTIFIER_HINTS = [
    re.compile(r"^0\d+$"),          # leading zeros (ZIP, IDs)
    re.compile(r"^\d{5}(-\d{4})?$"),  # ZIP
    re.compile(r"^\+?[\d\s\-().]{7,}$"),  # phone-like
    re.compile(r"^[A-Za-z]*\d{6,}$"),  # long numeric / SKU-ish
]


def _non_missing(df: pl.DataFrame, col: str, markers: list[str]) -> pl.Series:
    mask = df.select(missing_expr(col, markers)).to_series()
    return df[col].filter(~mask)


def _looks_like_identifier(values: list[str]) -> bool:
    if not values:
        return False
    hits = sum(1 for v in values if any(r.match(v) for r in IDENTIFIER_HINTS))
    return hits / len(values) >= 0.8


def _parse_compat(values: list[str], pattern: re.Pattern) -> tuple[int, list[str]]:
    ok = 0
    bad: list[str] = []
    for v in values:
        s = v.strip()
        if pattern.match(s.replace(",", "")):
            ok += 1
        else:
            if len(bad) < 5:
                bad.append(v)
    return ok, bad


def _date_compat(values: list[str]) -> tuple[str | None, str | None, list[str], int, bool]:
    """Returns (kind, fmt, bad_examples, bad_count, ambiguous)."""
    formats = [(f, tag) for f, tag in DATE_FORMATS]
    formats += [(f + sfx, tag) for f, tag in DATE_FORMATS for sfx in DATETIME_SUFFIXES]
    formats += [(f, "time") for f, _ in TIME_FORMATS]

    surviving = list(formats)
    bad: list[str] = []
    bad_count = 0
    any_matched = False
    # Dedupe + prefilter: strptime is expensive (~40us/call); every candidate
    # format requires at least one digit, so digit-free values can never match.
    uniq = list(dict.fromkeys(values[:200]))
    for v in uniq:
        s = v.strip()
        if not any(ch.isdigit() for ch in s):
            bad_count += 1
            if len(bad) < 5:
                bad.append(v)
            continue
        matched = [f for f, _ in surviving if _try_strptime(s, f)]
        if not matched:
            bad_count += 1
            if len(bad) < 5:
                bad.append(v)
            continue
        any_matched = True
        surviving = [(f, t) for f, t in surviving if f in matched]
        if not surviving:
            break
    if not any_matched or not surviving:
        return None, None, bad, bad_count, False
    fmt, tag = surviving[0]
    if tag == "time":
        kind = "time"
    else:
        kind = "datetime" if any(sfx in fmt for sfx in DATETIME_SUFFIXES) else "date"
    ambiguous = len({t for _, t in surviving if t and t != "time"}) > 1
    return kind, fmt, bad, bad_count, ambiguous


def _try_strptime(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def infer_column(df: pl.DataFrame, col: str, markers: list[str]) -> dict:
    dtype = df.schema[col]
    s = _non_missing(df, col, markers)
    n = s.len()
    result = {
        "column": col,
        "current_dtype": str(dtype),
        "non_missing": n,
        "suggested": "string",
        "compatible_pct": 100.0,
        "incompatible_count": 0,
        "incompatible_examples": [],
        "ambiguous_date_format": False,
        "date_format": None,
        "identifier_detected": False,
        "unique_count": None,
    }
    if n == 0:
        return result

    unique = s.n_unique()
    result["unique_count"] = unique

    if dtype == pl.Boolean:
        result["suggested"] = "boolean"
        return result
    if dtype in (pl.Date, pl.Datetime):
        result["suggested"] = "date" if dtype == pl.Date else "datetime"
        return result
    if is_numeric_dtype(dtype):
        result["suggested"] = "integer" if dtype.is_integer() else "decimal"
        # numeric column with identifier-like name still stays numeric here;
        # the identifier heuristic targets numeric-looking strings.
        return result

    # Inference runs on a bounded sample — regex/strptime per cell does not
    # scale; a few hundred values decide type reliably.
    values = [str(v) for v in s.head(300).to_list() if v is not None]
    m = len(values)

    # Boolean?
    bool_hits = sum(1 for v in values if v.strip().lower() in BOOL_TRUE | BOOL_FALSE)
    if bool_hits == m:
        result.update(suggested="boolean")
        return result

    # Integer / decimal?
    int_ok, int_bad = _parse_compat(values, INT_RE)
    float_ok, float_bad = _parse_compat(values, FLOAT_RE)
    if int_ok / m >= 0.95:
        if _looks_like_identifier(values):
            result.update(suggested="string", identifier_detected=True,
                          compatible_pct=100.0)
            return result
        result.update(suggested="integer", compatible_pct=int_ok / m * 100,
                      incompatible_count=m - int_ok, incompatible_examples=int_bad)
        return result
    if float_ok / m >= 0.95:
        result.update(suggested="decimal", compatible_pct=float_ok / m * 100,
                      incompatible_count=m - float_ok, incompatible_examples=float_bad)
        return result

    # Date / datetime?
    kind, fmt, date_bad, bad_n, ambiguous = _date_compat(values)
    scanned = len(dict.fromkeys(values[:200]))
    if kind and (scanned - bad_n) / max(scanned, 1) >= 0.9:
        result.update(
            suggested=kind,
            compatible_pct=(scanned - bad_n) / scanned * 100,
            incompatible_count=bad_n,
            incompatible_examples=date_bad,
            ambiguous_date_format=ambiguous,
            date_format=fmt,
        )
        return result

    # Categorical? (count AND proportion of unique values)
    if unique <= 50 and unique / n <= 0.5:
        result.update(suggested="categorical")
        return result

    result.update(suggested="string")
    return result


def assess_types(df: pl.DataFrame, markers: list[str], columns: list[str]) -> dict:
    return {"columns": [infer_column(df, c, markers) for c in columns]}
