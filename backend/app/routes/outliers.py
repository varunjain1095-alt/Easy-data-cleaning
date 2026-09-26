"""Phase 3B routes: outcome declaration, univariate screening, bivariate
contextual review, classification, and post-review treatments."""

import json
import time

import polars as pl
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import outliers
from ..jsonsafe import SafeJSONResponse
from ..masking import markers_param
from ..opspec import OperationSpec, Stage
from ..project import Project
from ..security import require_session
from ..sessions import Session, _atomic_write_json

router = APIRouter(prefix="/api/items/{item_id}", tags=["outliers"], default_response_class=SafeJSONResponse)


def _project_item(session: Session, item_id: str) -> tuple[Project, dict]:
    project = Project.load(session)
    if project is None:
        raise HTTPException(status_code=404, detail="No project in this session")
    item = project.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return project, item


def _save_meta(project: Project, item_id: str, meta: dict) -> None:
    _atomic_write_json(project.item_dir(item_id) / "meta.json", meta)


def _outcome(project: Project, item_id: str) -> dict | None:
    return project.item_meta(item_id).get("outcome")


# ---------------------------------------------------------------------------
# Outcome-variable declaration (8.1)
# ---------------------------------------------------------------------------


class OutcomeBody(BaseModel):
    column: str | None = None
    meaning: str | None = None
    positive_class: str | None = None


@router.post("/outcome")
def declare_outcome(item_id: str, body: OutcomeBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = project.history(item_id).working()
    meta = project.item_meta(item_id)
    if body.column is None:
        meta["outcome"] = None  # contextual bivariate still available
    else:
        if body.column not in df.columns:
            raise HTTPException(status_code=404, detail="Outcome column not found")
        meta["outcome"] = {
            "column": body.column,
            "meaning": body.meaning,
            "positive_class": body.positive_class,
            "dtype": str(df.schema[body.column]),
            "declared_at": time.time(),
        }
    _save_meta(project, item_id, meta)
    # outcome changes invalidate outcome-focused bivariate results only
    project.invalidate_dependents(item_id, Stage.OUTCOME)
    project.set_stage_state(item_id, Stage.OUTCOME, "completed" if body.column else "skipped")
    return {"outcome": meta["outcome"]}


@router.get("/outcome")
def get_outcome(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    return {"outcome": _outcome(project, item_id)}


# ---------------------------------------------------------------------------
# Stage 1: univariate screening (8.2)
# ---------------------------------------------------------------------------


@router.get("/outliers/screen/{column}")
def screen(
    item_id: str,
    column: str,
    method: str = "iqr",
    multiplier: float | None = None,
    threshold: float | None = None,
    lo: float | None = None,
    hi: float | None = None,
    min: float | None = None,
    max: float | None = None,
    session: Session = Depends(require_session),
):
    project, _ = _project_item(session, item_id)
    df = project.history(item_id).working()
    if column not in df.columns:
        raise HTTPException(status_code=404, detail="Column not found")
    params = {k: v for k, v in {
        "multiplier": multiplier, "threshold": threshold, "lo": lo, "hi": hi,
        "min": min, "max": max,
    }.items() if v is not None}
    try:
        result = outliers.univariate_screen(df, column, method, params)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # retain flagged ids in per-column state for later review/treatment gating
    outliers.set_column_state(project.item_dir(item_id), column, {
        "flagged_ids": [f["row_id"] for f in result["flagged"]],
        "flagged_values": result["flagged"],
        "bounds": result["bounds"],
        "screened_at": time.time(),
        "method": method,
        "params": params,
    })
    project.set_stage_state(item_id, Stage.UNIVARIATE, "in_progress")
    return result


class MarkBody(BaseModel):
    column: str
    action: str  # mark | keep_all | exclude  (only these allowed pre-review)


@router.post("/outliers/mark")
def mark(item_id: str, body: MarkBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    if body.action not in ("mark", "keep_all", "exclude"):
        raise HTTPException(status_code=400, detail="Univariate stage allows only: mark, keep_all, exclude")
    outliers.set_column_state(project.item_dir(item_id), body.column, {"univariate_decision": body.action})
    return outliers.load_state(project.item_dir(item_id))


# ---------------------------------------------------------------------------
# Stage 2: bivariate contextual review (8.3/8.4)
# ---------------------------------------------------------------------------


@router.get("/outliers/context/{column}")
def context_candidates(item_id: str, column: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = project.history(item_id).working()
    if column not in df.columns:
        raise HTTPException(status_code=404, detail="Column not found")
    markers = markers_param(None)
    candidates = outliers.recommend_context(df, column, markers, _outcome(project, item_id))
    return {"candidates": candidates, "outcome": _outcome(project, item_id)}


class BivariateBody(BaseModel):
    column: str
    context: str


@router.post("/outliers/bivariate")
def bivariate(item_id: str, body: BivariateBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = project.history(item_id).working()
    markers = markers_param(None)
    state = outliers.load_state(project.item_dir(item_id))
    col_state = state["columns"].get(body.column, {})
    flagged = col_state.get("flagged_ids") or []
    try:
        result = outliers.bivariate_review(df, body.column, body.context, flagged, markers)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    outliers.record_evidence(project.item_dir(item_id), body.column, body.context, result["evidence"])
    outliers.set_column_state(project.item_dir(item_id), body.column, {"bivariate_reviewed": True})
    project.set_stage_state(item_id, Stage.BIVARIATE, "in_progress")
    return result


class ClassifyBody(BaseModel):
    column: str
    row_id: int
    classification: str  # valid_extreme | likely_error | needs_review | excluded


@router.post("/outliers/classify")
def classify(item_id: str, body: ClassifyBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    if body.classification not in ("valid_extreme", "likely_error", "needs_review", "excluded"):
        raise HTTPException(status_code=400, detail="Invalid classification")
    state = outliers.classify_record(project.item_dir(item_id), body.column, body.row_id, body.classification)
    return {"state": state}


# ---------------------------------------------------------------------------
# Treatment after contextual review (8.5) - gated on bivariate review
# ---------------------------------------------------------------------------


class TreatBody(BaseModel):
    column: str
    row_ids: list[int]
    action: str  # keep | remove | set_missing | cap | impute | correct
    value: float | str | None = None      # for correct
    min: float | None = None              # for cap
    max: float | None = None              # for cap
    impute_method: str | None = None      # for impute: mean|median|mode


@router.post("/outliers/treat")
def treat(item_id: str, body: TreatBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    item_dir = project.item_dir(item_id)
    state = outliers.load_state(item_dir)
    col_state = state["columns"].get(body.column, {})
    if not col_state.get("bivariate_reviewed") and not col_state.get("classifications"):
        raise HTTPException(
            status_code=409,
            detail="Bivariate contextual review is required before outlier treatments (section 8.2/8.5).",
        )

    df = project.history(item_id).working()
    history = project.history(item_id)
    before_stats = None
    if body.action != "keep":
        try:
            before_stats = outliers.column_stats(df, body.column, markers_param(None))
        except ValueError:
            pass

    if body.action == "keep":
        pass  # explicit keep: recorded as a decision, no transformation
    elif body.action == "remove":
        spec = OperationSpec(op_type="drop_rows", stage=Stage.UNIVARIATE,
                             params={"row_ids": body.row_ids})
        df = history.apply(spec)
    elif body.action == "set_missing":
        spec = OperationSpec(op_type="treat_rows", stage=Stage.UNIVARIATE,
                             params={"column": body.column, "row_ids": body.row_ids, "method": "missing"})
        df = history.apply(spec)
    elif body.action == "cap":
        spec = OperationSpec(op_type="treat_rows", stage=Stage.UNIVARIATE,
                             params={"column": body.column, "row_ids": body.row_ids,
                                     "method": "cap", "min": body.min, "max": body.max})
        df = history.apply(spec)
    elif body.action == "impute":
        if body.impute_method not in ("mean", "median", "mode"):
            raise HTTPException(status_code=400, detail="impute_method must be mean, median, or mode")
        spec = OperationSpec(op_type="treat_rows", stage=Stage.UNIVARIATE,
                             params={"column": body.column, "row_ids": body.row_ids,
                                     "method": body.impute_method})
        df = history.apply(spec)
    elif body.action == "correct":
        if body.value is None:
            raise HTTPException(status_code=400, detail="correct requires a value")
        spec = OperationSpec(op_type="treat_rows", stage=Stage.UNIVARIATE,
                             params={"column": body.column, "row_ids": body.row_ids,
                                     "method": "value", "value": body.value})
        df = history.apply(spec)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")

    # record treated rows; re-screen stats for before/after comparison
    treated = set(col_state.get("treated_ids", [])) | set(body.row_ids)
    outliers.set_column_state(item_dir, body.column, {
        "treated_ids": sorted(treated),
        "treated_at": time.time(),
    })
    marked = project.invalidate_dependents(item_id, Stage.UNIVARIATE)
    after_stats = None
    if body.action != "keep":
        try:
            after_stats = outliers.column_stats(df, body.column, markers_param(None))
        except ValueError:
            pass
    return {
        "action": body.action,
        "invalidated": marked,
        "row_count": df.height,
        "distribution": {"before": before_stats, "after": after_stats},
    }


@router.get("/outliers/state")
def outlier_state(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    return outliers.load_state(project.item_dir(item_id))
