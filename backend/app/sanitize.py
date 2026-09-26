"""Spreadsheet formula-injection protection for CSV/XLSX exports.

Cells beginning with =, +, -, @, tab, CR, or whitespace followed by those
characters are prefixed with a single quote so spreadsheet apps treat them
as text.
"""

import polars as pl

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def sanitize_cell(value):
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped[:1] in _DANGEROUS_PREFIXES:
            return "'" + value
    return value


def sanitize_frame(df: pl.DataFrame) -> pl.DataFrame:
    """Apply formula-injection escaping to all String columns."""
    for c in df.columns:
        if df.schema[c] == pl.String:
            df = df.with_columns(
                pl.col(c).map_elements(sanitize_cell, return_dtype=pl.String).alias(c)
            )
    return df
