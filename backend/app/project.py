import json
import time
from pathlib import Path

import polars as pl

from .config import PREVIEW_ROWS, ROW_ID_COLUMN
from .history import DatasetHistory
from .invalidation import apply_invalidation
from .opspec import Stage
from .security import new_id
from .sessions import Session, _atomic_write_json

ITEM_STATUSES = {
    "not_started",
    "in_progress",
    "completed",
    "completed_with_warnings",
    "skipped",
}

STAGE_STATES = {
    "not_started",
    "in_progress",
    "completed",
    "skipped",
    "unresolved_warnings",
    "blocked_by_prerequisite",
    "needs_recalculation",
}

ALL_STAGES = [
    Stage.PROFILE,
    Stage.SPECIAL_CHARS,
    Stage.MISSINGNESS,
    Stage.UNITS,
    Stage.TYPES,
    Stage.NORMALIZATION,
    Stage.COLUMN_NAMES,
    Stage.INVALID_VALUES,
    Stage.STRUCTURES,
    Stage.PATTERNS,
    Stage.DUPLICATES,
    Stage.KEYS,
    Stage.OUTCOME,
    Stage.UNIVARIATE,
    Stage.BIVARIATE,
    Stage.VALIDATION,
]


class Project:
    """Workbook-level project containing independent per-item working datasets."""

    def __init__(self, session: Session, data: dict):
        self.session = session
        self.data = data

    @property
    def dir(self) -> Path:
        return self.session.path("project")

    @classmethod
    def create(cls, session: Session, source_filename: str, kind: str) -> "Project":
        data = {
            "project_id": new_id(),
            "kind": kind,
            "source_filename": source_filename,
            "items": [],
            "created_at": time.time(),
        }
        project = cls(session, data)
        project._persist()
        return project

    @classmethod
    def load(cls, session: Session) -> "Project | None":
        path = session.path("project", "project.json")
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(session, data)

    def _persist(self) -> None:
        _atomic_write_json(self.dir / "project.json", self.data)

    # -- items ---------------------------------------------------------------

    def item_dir(self, item_id: str) -> Path:
        return self.dir / "items" / item_id

    def add_item(self, name: str, item_type: str) -> dict:
        item = {
            "item_id": new_id()[:12],
            "name": name,
            "item_type": item_type,  # "file" | "sheet" | "table"
            "status": "not_started",
            "rejected_reason": None,
            "row_count": None,
            "column_count": None,
        }
        self.data["items"].append(item)
        self.item_dir(item["item_id"]).mkdir(parents=True, exist_ok=True)
        meta = {"stage_states": {s: "not_started" for s in ALL_STAGES}}
        _atomic_write_json(self.item_dir(item["item_id"]) / "meta.json", meta)
        self._persist()
        return item

    def reject_item(self, item_id: str, reason: str) -> None:
        item = self.get_item(item_id)
        if item:
            item["status"] = "rejected"
            item["rejected_reason"] = reason
            self._persist()

    def get_item(self, item_id: str) -> dict | None:
        for item in self.data["items"]:
            if item["item_id"] == item_id:
                return item
        return None

    def item_meta(self, item_id: str) -> dict:
        path = self.item_dir(item_id) / "meta.json"
        if not path.exists():
            return {"stage_states": {s: "not_started" for s in ALL_STAGES}}
        return json.loads(path.read_text(encoding="utf-8"))

    def set_item_dims(self, item_id: str, rows: int, cols: int) -> None:
        item = self.get_item(item_id)
        if item:
            item["row_count"] = rows
            item["column_count"] = cols
            self._persist()

    def set_item_status(self, item_id: str, status: str) -> None:
        item = self.get_item(item_id)
        if item and status in ITEM_STATUSES | {"rejected"}:
            item["status"] = status
            self._persist()

    def set_stage_state(self, item_id: str, stage: str, state: str) -> None:
        if state not in STAGE_STATES:
            raise ValueError(f"Invalid stage state: {state}")
        meta = self.item_meta(item_id)
        meta["stage_states"][stage] = state
        _atomic_write_json(self.item_dir(item_id) / "meta.json", meta)

    def set_item_extra(self, item_id: str, key: str, value) -> None:
        meta = self.item_meta(item_id)
        meta.setdefault("extra", {})[key] = value
        _atomic_write_json(self.item_dir(item_id) / "meta.json", meta)

    def item_extra(self, item_id: str, key: str, default=None):
        return self.item_meta(item_id).get("extra", {}).get(key, default)

    def invalidate_dependents(self, item_id: str, changed_stage: str) -> list[str]:
        meta = self.item_meta(item_id)
        marked = apply_invalidation(meta["stage_states"], changed_stage)
        _atomic_write_json(self.item_dir(item_id) / "meta.json", meta)
        return marked

    def history(self, item_id: str) -> DatasetHistory:
        return DatasetHistory(self.item_dir(item_id))

    # -- views -----------------------------------------------------------------

    def to_dict(self) -> dict:
        items = []
        for item in self.data["items"]:
            entry = dict(item)
            entry["stage_states"] = self.item_meta(item["item_id"])["stage_states"]
            items.append(entry)
        return {**self.data, "items": items}

    def preview(self, item_id: str, n: int = PREVIEW_ROWS) -> dict | None:
        item = self.get_item(item_id)
        if item is None:
            return None
        working = self.item_dir(item_id) / "working.parquet"
        if not working.exists():
            return None
        df = pl.read_parquet(working)
        display_cols = [c for c in df.columns if c != ROW_ID_COLUMN]
        head = df.select(display_cols).head(n)
        return {
            "item_id": item_id,
            "name": item["name"],
            "row_count": df.height,
            "column_count": len(display_cols),
            "columns": [
                {"name": c, "dtype": str(df.schema[c])} for c in display_cols
            ],
            "rows": head.to_dicts(),
        }
