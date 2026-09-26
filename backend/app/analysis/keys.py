"""Uniqueness & key validation (post-duplicates stage).

A declared candidate key (one column or a combination) must be unique and
non-missing. The assessment separates three failure modes:

  missing     - rows where any key column is a recognized missing value
  identical   - repeated keys whose other attributes are all equal
  conflicting - repeated keys with differing attributes (columns listed)

Comparison is dtype-faithful: keys are compared in their declared dtype,
never coerced — a string ID '00417' stays distinct from '417'.
"""

import polars as pl

from ..config import ROW_ID_COLUMN
from ..masking import missing_expr


def assess_keys(df: pl.DataFrame, columns: list[str], markers: list[str],
                conflict_sample: int = 50) -> dict:
    if not columns:
        return {"error": "Pick at least one key column"}
    for c in columns:
        if c not in df.columns:
            return {"error": f"Unknown column: {c}"}

    other_cols = [c for c in df.columns if c not in columns and c != ROW_ID_COLUMN]

    # rows where any key component is missing
    miss_mask = pl.lit(False)
    for c in columns:
        miss_mask = miss_mask | missing_expr(c, markers)
    missing_rows = df.filter(miss_mask)
    missing = {
        "count": missing_rows.height,
        "row_ids": missing_rows[ROW_ID_COLUMN].head(50).to_list(),
        "samples": missing_rows.select(columns).head(5).to_dicts(),
    }

    valid = df.filter(~miss_mask)
    # group rows by the key combination — compare in native dtype, no coercion
    groups: dict[tuple, list[int]] = {}
    keys_list = list(zip(*[valid[c].to_list() for c in columns])) if columns else []
    rids = valid[ROW_ID_COLUMN].to_list()
    for k, rid in zip(keys_list, rids):
        groups.setdefault(k, []).append(rid)

    identical, conflicting = [], []
    for k, ids in groups.items():
        if len(ids) < 2:
            continue
        key_dict = {columns[i]: k[i] for i in range(len(columns))}
        sub = df.filter(pl.col(ROW_ID_COLUMN).is_in(ids))
        # identical iff every non-key column has <=1 distinct value in the group
        diffs = {}
        for c in other_cols:
            vals = [v for v in sub[c].to_list()]
            if len({repr(v) for v in vals}) > 1:
                diffs[c] = vals
        entry = {"key": key_dict, "row_ids": ids}
        if diffs:
            entry["conflicts"] = diffs
            conflicting.append(entry)
        else:
            identical.append(entry)

    return {
        "columns": columns,
        "total_rows": df.height,
        "missing": missing,
        "repeated_identical": {
            "count": len(identical),
            "groups": identical[:conflict_sample],
        },
        "repeated_conflicting": {
            "count": len(conflicting),
            "groups": conflicting[:conflict_sample],
        },
        "unique_rows": sum(1 for ids in groups.values() if len(ids) == 1),
    }
