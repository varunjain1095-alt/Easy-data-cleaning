"""Final-validation scaffolding (Phase 3A).

Re-runs applicable quality checks against the CURRENT cleaned dataset and
compares it to the immutable original - never relies on results calculated
earlier in the workflow. Outlier counts are placeholders until Phase 3B.
"""

import json
import time
from pathlib import Path

import polars as pl

from . import outliers
from .analysis import keys
from .config import ROW_ID_COLUMN
from .duplicates import _value_groups
from .jobs import Job
from .masking import markers_param, missing_count, normalize_missing
from .opspec import Stage
from .project import Project
from .sessions import Session, _atomic_write_json


def _missing_total(df: pl.DataFrame, markers: list[str]) -> int:
    return sum(
        missing_count(df, c, markers)
        for c in df.columns
        if c != ROW_ID_COLUMN
    )


def _duplicate_rows(df: pl.DataFrame) -> int:
    """Rows involved in exact duplicates across all display columns."""
    cols = [c for c in df.columns if c != ROW_ID_COLUMN]
    if not cols:
        return 0
    for c in cols:
        df = normalize_missing(df, c, markers_param(None))
    groups = _value_groups(df, cols)
    return sum(len(g) - 1 for g in groups)


def _cells_changed(orig: pl.DataFrame, work: pl.DataFrame) -> tuple[int, int]:
    """(changed_cells, changed_rows) over shared rows/columns."""
    shared_cols = [
        c for c in orig.columns
        if c in work.columns and c != ROW_ID_COLUMN
    ]
    if not shared_cols:
        return 0, 0
    # compare as strings so dtype conversions register as changed cells
    orig_sel = orig.select([ROW_ID_COLUMN] + shared_cols).with_columns(
        [pl.col(c).cast(pl.String).alias(c) for c in shared_cols]
    )
    work_sel = work.select([ROW_ID_COLUMN] + shared_cols).with_columns(
        [pl.col(c).cast(pl.String).alias(c) for c in shared_cols]
    )
    joined = orig_sel.join(work_sel, on=ROW_ID_COLUMN, how="inner", suffix="__w")
    diff = pl.lit(False)
    for c in shared_cols:
        diff = diff | pl.col(c).ne_missing(pl.col(f"{c}__w"))
    rows_changed = int(joined.filter(diff).height)
    cells = 0
    if rows_changed:
        for c in shared_cols:
            cells += int(
                joined.filter(pl.col(c).ne_missing(pl.col(f"{c}__w"))).height
            )
    return cells, rows_changed


def run_validation(job: Job, session: Session, item_id: str) -> dict:
    project = Project.load(session)
    if project is None or project.get_item(item_id) is None:
        raise ValueError("Item not found")

    job.update_progress("Loading datasets")
    history = project.history(item_id)
    orig = history.original()
    work = history.working()
    markers = markers_param(None)

    job.update_progress("Counting missing values", done=1, total=4)
    missing_before = _missing_total(orig, markers)
    missing_after = _missing_total(work, markers)

    job.update_progress("Checking duplicates", done=2, total=4)
    dup_before = _duplicate_rows(orig)
    dup_after = _duplicate_rows(work)

    job.update_progress("Comparing datasets", done=3, total=4)
    cells_changed, rows_changed = _cells_changed(orig, work)

    orig_cols = {c for c in orig.columns if c != ROW_ID_COLUMN}
    work_cols = {c for c in work.columns if c != ROW_ID_COLUMN}
    item = project.get_item(item_id)
    meta = project.item_meta(item_id)
    stage_states = meta["stage_states"]

    # Re-check any declared key against the current working frame
    key_decl = meta.get("extra", {}).get("key_declaration")
    key_check = None
    if key_decl:
        key_cols = [c for c in key_decl.get("columns", []) if c in work.columns]
        if key_cols:
            r = keys.assess_keys(work, key_cols, markers)
            key_check = {
                "columns": key_decl["columns"],
                "missing": r["missing"]["count"],
                "repeated_identical": r["repeated_identical"]["count"],
                "repeated_conflicting": r["repeated_conflicting"]["count"],
                "ok": r["missing"]["count"] == 0
                and r["repeated_identical"]["count"] == 0
                and r["repeated_conflicting"]["count"] == 0,
            }
        else:
            key_check = {"columns": key_decl["columns"], "ok": False,
                         "error": "declared key column(s) no longer present"}

    result = {
        "item_id": item_id,
        "validated_at": time.time(),
        "row_count": {"before": orig.height, "after": work.height},
        "column_count": {"before": len(orig_cols), "after": len(work_cols)},
        "missing_values": {"before": missing_before, "after": missing_after},
        "duplicate_rows": {"before": dup_before, "after": dup_after},
        "cells_changed": cells_changed,
        "rows_changed": rows_changed,
        "rows_removed": max(orig.height - work.height, 0),
        "columns_removed": sorted(orig_cols - work_cols),
        "columns_added": sorted(work_cols - orig_cols),
        "flagged_outliers_unresolved": outliers.unresolved_outliers(project.item_dir(item_id)),
        "key_validation": key_check,
        "stage_states": stage_states,
        "unresolved_warnings": [
            f"stage '{s}' marked needs_recalculation"
            for s, st in stage_states.items()
            if st == "needs_recalculation"
        ] + ([f"declared key {key_check['columns']} violated"] if key_check and not key_check.get("ok") else []),
        "transformations_applied": history.pointer,
        "transformations_undone": len(history.ops) - history.pointer,
    }
    _atomic_write_json(project.item_dir(item_id) / "validation.json", result)
    project.set_stage_state(item_id, Stage.VALIDATION, "completed")
    job.update_progress("Done", done=4, total=4)
    return result


def load_validation(item_dir: Path) -> dict | None:
    p = item_dir / "validation.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
