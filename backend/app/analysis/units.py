"""Units & scales assessment (pre–data-types stage).

Detects unit tokens carried inside values — prefixes (€100, USD 100) and
suffixes (5kg, 10 lb, 25%) — plus a possible-mixed-scale hint for pure
numeric columns. Detection is advisory: conversion is always user-confirmed
per rule, and unmatched values are left unchanged and reported.
"""

import re

import polars as pl

from ..config import ROW_ID_COLUMN
from ..masking import is_numeric_dtype, missing_expr, markers_param

# value -> (prefix_unit, numeric_part, suffix_unit); None if no split applies
_SPLIT_RE = re.compile(
    r"^\s*([^\d\s.,+\-]*)\s*([+-]?[\d]+(?:[.,]\d+)?)\s*([^\d]*)\s*$"
)
_NUM_ONLY_RE = re.compile(r"^\s*[+-]?[\d]+(?:[.,]\d+)?\s*$")

# Canonical suggestions only; currency factors are never prefilled.
KNOWN_FACTORS: dict[str, dict[str, float]] = {
    "kg": {"g": 1000.0, "lb": 2.20462},
    "g": {"kg": 0.001, "lb": 0.00220462},
    "lb": {"kg": 0.453592, "g": 453.592},
    "m": {"cm": 100.0, "mm": 1000.0, "km": 0.001},
    "cm": {"m": 0.01, "mm": 10.0},
    "mm": {"m": 0.001, "cm": 0.1},
    "km": {"m": 1000.0},
    "l": {"ml": 1000.0},
    "ml": {"l": 0.001},
}
CURRENCY_TOKENS = {"$", "€", "£", "¥", "usd", "eur", "gbp", "jpy"}


def _split(value: str) -> tuple[str | None, float, str | None] | None:
    """Split ' 10 lb ' -> (None, 10.0, 'lb'), '€100' -> ('€', 100.0, None)."""
    m = _SPLIT_RE.match(value)
    if not m:
        return None
    pre, num, suf = m.group(1) or None, m.group(2), m.group(3) or None
    try:
        n = float(num.replace(",", "."))
    except ValueError:
        return None
    return (pre.strip() if pre else None, n, suf.strip() if suf else None)


def _canon(unit: str) -> str:
    return unit.strip().lower()


def assess_units(df: pl.DataFrame, column: str, markers: list[str]) -> dict:
    """Detect unit tokens and scale hints for one column.

    Only values carrying a unit token are grouped; recognized-missing values
    are excluded from all counts. Pure numeric columns get a scale hint only.
    """
    if column not in df.columns:
        return {"error": f"Unknown column: {column}"}
    dtype = df.schema[column]
    mask = missing_expr(column, markers)
    total = df.height

    if is_numeric_dtype(dtype):
        vals = df.filter(~mask)[column].drop_nulls()
        n = vals.len()
        in_frac = int(vals.filter((vals > 0) & (vals <= 1)).len())
        in_big = int(vals.filter(vals > 1).len())
        hint = None
        if n >= 10 and in_frac >= 2 and in_big >= 2 and vals.max() <= 100:
            hint = {
                "kind": "possible_mixed_scale",
                "note": (
                    f"{in_frac} values sit in 0–1 and {in_big} sit above 1 "
                    "(max 100) — possibly mixed percent scales (0.25 vs 25). "
                    "Not auto-decided: supply a condition if this is real."
                ),
            }
        return {
            "column": column, "dtype": str(dtype), "total_rows": total,
            "groups": [], "plain": {"count": n, "min": vals.min(), "max": vals.max()},
            "unmatched": {"count": 0, "samples": []}, "scale_hint": hint,
        }

    if dtype != pl.String:
        return {"column": column, "dtype": str(dtype), "total_rows": total,
                "groups": [], "plain": None, "unmatched": {"count": 0, "samples": []},
                "scale_hint": None}

    groups: dict[str, dict] = {}
    plain: list[float] = []
    unmatched: list[str] = []
    for v in df.filter(~mask)[column].drop_nulls().to_list():
        s = str(v)
        if _NUM_ONLY_RE.match(s):
            plain.append(float(s.replace(",", ".")))
            continue
        parts = _split(s)
        if parts is None:
            if len(unmatched) < 10:
                unmatched.append(s)
            groups.setdefault("__unmatched__", {"count": 0})["count"] += 1
            continue
        pre, num, suf = parts
        unit, pos = (pre, "prefix") if pre else (suf, "suffix")
        if not unit:
            plain.append(num)
            continue
        key = _canon(unit)
        g = groups.setdefault(
            key,
            {"unit": unit, "canonical": key, "position": pos, "count": 0,
             "min": num, "max": num, "samples": [], "is_currency": key in CURRENCY_TOKENS,
             "suggested_factors": KNOWN_FACTORS.get(key, {})},
        )
        g["count"] += 1
        g["min"] = min(g["min"], num)
        g["max"] = max(g["max"], num)
        if len(g["samples"]) < 3 and s not in g["samples"]:
            g["samples"].append(s)

    out_groups = [g for k, g in sorted(groups.items()) if k != "__unmatched__"]
    unmatched_count = groups.get("__unmatched__", {}).get("count", 0)
    return {
        "column": column, "dtype": str(dtype), "total_rows": total,
        "groups": sorted(out_groups, key=lambda g: -g["count"]),
        "plain": {
            "count": len(plain),
            "min": min(plain) if plain else None,
            "max": max(plain) if plain else None,
        },
        "unmatched": {"count": unmatched_count, "samples": unmatched},
        "scale_hint": None,
    }


def list_columns(df: pl.DataFrame, markers: list[str]) -> list[str]:
    """Columns worth assessing: string columns + numeric columns (scale hints)."""
    return [c for c in df.columns if c != ROW_ID_COLUMN]


def markers() -> list[str]:
    return markers_param(None)
