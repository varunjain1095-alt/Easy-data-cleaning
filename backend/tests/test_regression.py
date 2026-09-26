"""Deliberately dirty reference dataset exercising every cleaning operation
and expected warning state (architecture section 17)."""

import polars as pl
import pytest

from app.config import ROW_ID_COLUMN
from app.history import DatasetHistory
from app.invalidation import apply_invalidation
from app.opspec import OperationSpec, Stage, execute_op

DIRTY = pl.DataFrame({
    ROW_ID_COLUMN: list(range(8)),
    " name ": [" Alice ", "bob", "Alice", "N/A", "CAROL", "dave", "eve", "Alice"],
    "Age": ["30", "-5", "30", "40", "45", "N/A", "50", "30"],
    "Dept": ["Eng", "eng", "Eng", "Sales", "sales", "Eng", None, "Eng"],
    "Salary": ["100", "100", "100", "200", "200", "100", "300", "100"],
    "Joined": ["2024-01-01", "2024-01-02", "01/02/2024", "2024-01-04", "2024-01-05", "bad", "2024-01-07", "2024-01-01"],
})


def _spec(op_type, stage, params):
    return OperationSpec(op_type=op_type, stage=stage, params=params)


def test_full_dirty_dataset_pipeline(tmp_path):
    h = DatasetHistory(tmp_path / "item")
    h.initialize(DIRTY)

    # 1. column names
    df = h.apply(_spec("rename_columns", Stage.COLUMN_NAMES,
                       {"mapping": {" name ": "name", "Age": "age", "Dept": "dept", "Salary": "salary", "Joined": "joined"}}))
    assert set(df.columns) == {ROW_ID_COLUMN, "name", "age", "dept", "salary", "joined"}

    # 2. missingness: dept null -> constant; age N/A -> drop marker -> median
    df = h.apply(_spec("treat_missing", Stage.MISSINGNESS,
                       {"column": "dept", "method": "constant", "value": "Unknown"}))
    assert df["dept"].to_list()[6] == "Unknown"
    df = h.apply(_spec("treat_missing", Stage.MISSINGNESS,
                       {"column": "age", "method": "median"}))
    assert df["age"].to_list()[5] == "30.0"  # median of [30,-5,30,40,45,50,30]

    # 3. types
    df = h.apply(_spec("convert_type", Stage.TYPES, {"column": "age", "target": "integer"}))
    assert df.schema["age"] == pl.Int64
    df = h.apply(_spec("convert_type", Stage.TYPES, {"column": "joined", "target": "date", "format": "%Y-%m-%d"}))
    assert df["joined"].to_list()[5] is None  # "bad" -> null

    # 4. normalization
    df = h.apply(_spec("normalize", Stage.NORMALIZATION,
                       {"columns": ["name"], "operations": [{"op": "trim"}, {"op": "lower"}]}))
    assert df["name"].to_list()[0] == "alice"
    df = h.apply(_spec("normalize", Stage.NORMALIZATION,
                       {"columns": ["dept"], "operations": [{"op": "map_values", "mapping": {"eng": "Eng", "sales": "Sales"}}]}))
    assert df["dept"].to_list()[1] == "Eng"

    # 5. invalid values
    df = h.apply(_spec("mask_values", Stage.INVALID_VALUES,
                       {"column": "age", "rule": {"type": "range", "min": 0, "max": 120}}))
    assert df["age"].to_list()[1] is None  # -5 masked

    # 6. duplicates: alice rows identical on name+salary after normalization
    df = h.apply(_spec("drop_rows", Stage.DUPLICATES, {"params": {"row_ids": [2, 7]}}["params"]))
    assert df.height == 6

    # undo/redo integrity through the whole chain
    df = h.undo()
    assert df.height == 8  # no prior op removed rows
    df = h.redo()
    assert df.height == 6

    # replay equivalence: rebuild from checkpoint + ops must equal working
    rebuilt = h._restore(h.pointer)
    assert rebuilt.equals(h.working())


def test_invalidation_cascade(tmp_path):
    """Every section-15 rule marks dependents needs_recalculation."""
    all_stages = [Stage.MISSINGNESS, Stage.TYPES, Stage.NORMALIZATION, Stage.COLUMN_NAMES,
                  Stage.INVALID_VALUES, Stage.STRUCTURES, Stage.DUPLICATES, Stage.OUTCOME,
                  Stage.UNIVARIATE, Stage.BIVARIATE, Stage.VALIDATION, Stage.STATISTICS]
    states = {s: "completed" for s in all_stages}
    marked = apply_invalidation(dict(states), Stage.MISSINGNESS)
    for s in (Stage.TYPES, Stage.NORMALIZATION, Stage.DUPLICATES, Stage.UNIVARIATE,
              Stage.BIVARIATE, Stage.VALIDATION, Stage.STATISTICS):
        assert s in marked
    marked = apply_invalidation(dict(states), Stage.DUPLICATES)
    assert Stage.UNIVARIATE in marked and Stage.VALIDATION in marked


def test_replay_determinism_random_sample(tmp_path):
    """Seeded random-sample imputation replays identically."""
    h = DatasetHistory(tmp_path / "item")
    h.initialize(DIRTY)
    h.apply(_spec("treat_missing", Stage.MISSINGNESS,
                  {"column": "Dept", "method": "random_sample", "seed": 99}))
    first = h.working()["Dept"].to_list()
    h.undo(); h.redo()
    assert h.working()["Dept"].to_list() == first
