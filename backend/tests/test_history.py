import polars as pl

from app.config import ROW_ID_COLUMN
from app.history import DatasetHistory
from app.opspec import OperationSpec, Stage


def _frame():
    return pl.DataFrame(
        {
            ROW_ID_COLUMN: [0, 1, 2, 3],
            "name": ["a", "b", "c", "d"],
            "val": [1, None, 3, 4],
        }
    )


def _spec(op_type, params, stage=Stage.MISSINGNESS):
    return OperationSpec(op_type=op_type, stage=stage, params=params)


def test_apply_undo_redo(tmp_path):
    h = DatasetHistory(tmp_path / "item")
    h.initialize(_frame())

    h.apply(_spec("rename_column", {"from": "name", "to": "label"}, Stage.COLUMN_NAMES))
    h.apply(_spec("fill_null_constant", {"column": "val", "value": 0}))
    h.apply(_spec("drop_rows", {"row_ids": [3]}, Stage.STRUCTURES))

    df = h.working()
    assert "label" in df.columns and "name" not in df.columns
    assert df.height == 3
    assert df.filter(pl.col(ROW_ID_COLUMN) == 1)["val"].item() == 0

    df = h.undo()
    assert df.height == 4  # row 3 restored
    df = h.undo()
    assert df.filter(pl.col(ROW_ID_COLUMN) == 1)["val"].is_null().item()
    df = h.redo()
    assert df.filter(pl.col(ROW_ID_COLUMN) == 1)["val"].item() == 0
    assert h.can_undo() and h.can_redo()


def test_apply_after_undo_truncates_redo_tail(tmp_path):
    h = DatasetHistory(tmp_path / "item")
    h.initialize(_frame())
    h.apply(_spec("drop_rows", {"row_ids": [0]}))
    h.apply(_spec("drop_rows", {"row_ids": [1]}))
    h.undo()
    h.apply(_spec("drop_rows", {"row_ids": [2]}))
    assert not h.can_redo()
    assert len(h.ops) == 2
    remaining = set(h.working()[ROW_ID_COLUMN].to_list())
    assert remaining == {1, 3}


def test_checkpoint_replay_equivalence(tmp_path):
    """Working state after checkpoint+replay must equal linear application."""
    import app.history as hist_mod

    original_interval = hist_mod.CHECKPOINT_INTERVAL
    hist_mod.CHECKPOINT_INTERVAL = 3
    try:
        h = DatasetHistory(tmp_path / "item")
        h.initialize(_frame())
        for i in range(7):
            h.apply(_spec("fill_null_constant", {"column": "val", "value": i}))
        linear = h.working()
        # Force a rebuild from the closest checkpoint (cp at seq 3 + replay 4 ops).
        rebuilt = h._restore(h.pointer)
        assert linear.equals(rebuilt)
        assert 3 in h.checkpoints or 6 in h.checkpoints
    finally:
        hist_mod.CHECKPOINT_INTERVAL = original_interval


def test_history_persists_across_loads(tmp_path):
    h = DatasetHistory(tmp_path / "item")
    h.initialize(_frame())
    h.apply(_spec("drop_rows", {"row_ids": [0]}))

    h2 = DatasetHistory(tmp_path / "item")
    assert len(h2.ops) == 1
    assert h2.pointer == 1
    assert h2.working().height == 3
