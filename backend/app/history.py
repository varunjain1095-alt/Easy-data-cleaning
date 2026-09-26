import json
import threading
from pathlib import Path

import polars as pl

from .config import CHECKPOINT_INTERVAL
from .opspec import OperationSpec, execute_op
from .sessions import _atomic_write_json


class DatasetHistory:
    """Hybrid checkpoint-and-replay transformation model for one dataset item.

    - The uploaded original is immutable (original.parquet).
    - Each confirmed transformation is stored as a deterministic OperationSpec
      in an ordered log with an undo/redo pointer.
    - Working-dataset checkpoints are written every CHECKPOINT_INTERVAL ops.
    - Restoring loads the closest checkpoint and replays later operations.
    - No complete dataset copy is stored after every transformation.
    """

    def __init__(self, item_dir: Path):
        self.item_dir = item_dir
        self.history_path = item_dir / "history.json"
        self.checkpoints_dir = item_dir / "checkpoints"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.ops: list[OperationSpec] = []
        self.pointer = 0  # number of applied ops; ops[pointer:] are redoable
        self.checkpoints: list[int] = []  # op seqs that have checkpoints
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self.history_path.exists():
            return
        data = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.ops = [OperationSpec.from_dict(o) for o in data.get("ops", [])]
        self.pointer = data.get("pointer", len(self.ops))
        self.checkpoints = data.get("checkpoints", [])

    def _persist(self) -> None:
        _atomic_write_json(
            self.history_path,
            {
                "ops": [o.to_dict() for o in self.ops],
                "pointer": self.pointer,
                "checkpoints": self.checkpoints,
            },
        )

    def _checkpoint_path(self, seq: int) -> Path:
        return self.checkpoints_dir / f"cp_{seq:06d}.parquet"

    def _write_checkpoint(self, df: pl.DataFrame, seq: int) -> None:
        df.write_parquet(self._checkpoint_path(seq))
        if seq not in self.checkpoints:
            self.checkpoints.append(seq)
            self.checkpoints.sort()

    # -- dataset access -------------------------------------------------------

    @property
    def original_path(self) -> Path:
        return self.item_dir / "original.parquet"

    @property
    def working_path(self) -> Path:
        return self.item_dir / "working.parquet"

    def original(self) -> pl.DataFrame:
        return pl.read_parquet(self.original_path)

    def working(self) -> pl.DataFrame:
        return pl.read_parquet(self.working_path)

    def initialize(self, df: pl.DataFrame) -> None:
        """Store the immutable original and the initial working dataset."""
        df.write_parquet(self.original_path)
        df.write_parquet(self.working_path)
        self._write_checkpoint(df, 0)  # checkpoint 0 = pristine original
        self._persist()

    # -- operations -----------------------------------------------------------

    def apply(self, spec: OperationSpec) -> pl.DataFrame:
        """Confirm a transformation: append to log, execute, persist, maybe checkpoint."""
        with self._lock:
            if self.pointer < len(self.ops):
                # Applying a new op after undos drops the redo tail.
                self.ops = self.ops[: self.pointer]
            spec.seq = self.pointer + 1
            df = self.working()
            df = execute_op(df, spec)
            self.ops.append(spec)
            self.pointer += 1
            df.write_parquet(self.working_path)
            if self.pointer % CHECKPOINT_INTERVAL == 0:
                self._write_checkpoint(df, self.pointer)
            self._persist()
            return df

    def _restore(self, target_pointer: int) -> pl.DataFrame:
        """Rebuild working state at target_pointer via nearest checkpoint + replay."""
        eligible = [s for s in self.checkpoints if s <= target_pointer]
        start = max(eligible) if eligible else 0
        if start == 0:
            df = self.original()
        else:
            df = pl.read_parquet(self._checkpoint_path(start))
        for spec in self.ops[start:target_pointer]:
            df = execute_op(df, spec)
        df.write_parquet(self.working_path)
        return df

    def undo(self) -> pl.DataFrame | None:
        """Reverse the most recently applied transformation. Returns working df or None."""
        with self._lock:
            if self.pointer == 0:
                return None
            self.pointer -= 1
            df = self._restore(self.pointer)
            self._persist()
            return df

    def redo(self) -> pl.DataFrame | None:
        """Reapply the most recently undone transformation."""
        with self._lock:
            if self.pointer >= len(self.ops):
                return None
            self.pointer += 1
            df = self._restore(self.pointer)
            self._persist()
            return df

    # -- introspection ----------------------------------------------------------

    def can_undo(self) -> bool:
        return self.pointer > 0

    def can_redo(self) -> bool:
        return self.pointer < len(self.ops)

    def to_dict(self) -> dict:
        return {
            "ops": [o.to_dict() for o in self.ops],
            "pointer": self.pointer,
            "can_undo": self.can_undo(),
            "can_redo": self.can_redo(),
            "checkpoints": self.checkpoints,
        }
