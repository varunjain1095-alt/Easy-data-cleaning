"""Phase 2 cleaning endpoints: assessments, op preview, duplicates, stages."""

import polars as pl
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..analysis import basic, keys, missingness, normalize, patterns, profile, special_chars, typing as type_analysis, units
from ..duplicates import (
    analyze_duplicates,
    completeness,
    estimate_candidates,
    load_result,
    save_result,
)
from ..jobs import Job, job_registry
from ..jsonsafe import SafeJSONResponse
from ..masking import markers_param, missing_expr
from ..opspec import OP_REGISTRY, OperationSpec, Stage, execute_op
from ..project import ALL_STAGES, Project
from ..security import require_session
from ..sessions import Session

router = APIRouter(prefix="/api/items/{item_id}", tags=["cleaning"], default_response_class=SafeJSONResponse)


def _project_item(session: Session, item_id: str) -> tuple[Project, dict]:
    project = Project.load(session)
    if project is None:
        raise HTTPException(status_code=404, detail="No project in this session")
    item = project.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return project, item


def _working_df(project: Project, item_id: str) -> pl.DataFrame:
    return project.history(item_id).working()


# Assessment results are keyed on (item, stage, op-pointer): any applied,
# undone, or redone op changes the pointer and invalidates cached results.
_ASSESS_CACHE: dict[tuple, dict] = {}
_ASSESS_CACHE_MAX = 500


def _cached_assess(project: Project, item_id: str, stage: str, fn):
    key = (item_id, stage, project.history(item_id).pointer)
    if key not in _ASSESS_CACHE:
        if len(_ASSESS_CACHE) >= _ASSESS_CACHE_MAX:
            _ASSESS_CACHE.clear()
        _ASSESS_CACHE[key] = fn()
    return _ASSESS_CACHE[key]


# ---------------------------------------------------------------------------
# Assessments (read-only; never mutate)
# ---------------------------------------------------------------------------


@router.get("/assess/profile")
def assess_profile(item_id: str, session: Session = Depends(require_session)):
    project, _item = _project_item(session, item_id)
    return _cached_assess(project, item_id, "profile",
                          lambda: profile.assess_profile(_working_df(project, item_id)))


@router.get("/assess/special-chars")
def assess_special_chars(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    return _cached_assess(project, item_id, "special-chars",
                          lambda: special_chars.assess_special_chars(_working_df(project, item_id)))


@router.get("/assess/missingness")
def assess_missingness(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    return _cached_assess(project, item_id, "missingness",
                          lambda: missingness.assess_missingness(_working_df(project, item_id)))


@router.get("/assess/units")
def assess_units(item_id: str, column: str | None = None, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = _working_df(project, item_id)
    if column is None:
        return {"columns": units.list_columns(df, markers_param(None))}
    r = units.assess_units(df, column, markers_param(None))
    if "error" in r:
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@router.get("/assess/patterns")
def assess_patterns(item_id: str, column: str, pattern: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    r = patterns.assess_pattern(_working_df(project, item_id), column, pattern, markers_param(None))
    if "error" in r:
        raise HTTPException(status_code=422, detail=r["error"])
    return r


@router.get("/assess/pattern-presets")
def pattern_presets(item_id: str, session: Session = Depends(require_session)):
    _project_item(session, item_id)
    return {"presets": sorted(patterns.PATTERN_PRESETS)}


class KeyDeclareBody(BaseModel):
    columns: list[str] | None


@router.post("/keys/declare")
def declare_key(item_id: str, body: KeyDeclareBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    project.set_item_extra(item_id, "key_declaration", {"columns": body.columns} if body.columns else None)
    return {"declared": body.columns}


@router.get("/keys/declaration")
def get_key_declaration(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    return {"declared": project.item_extra(item_id, "key_declaration")}


@router.get("/assess/keys")
def assess_keys(item_id: str, columns: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    r = keys.assess_keys(_working_df(project, item_id), [c for c in columns.split(",") if c], markers_param(None))
    if "error" in r:
        raise HTTPException(status_code=422, detail=r["error"])
    return r


@router.get("/assess/types")
def assess_types(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    def _run():
        df = _working_df(project, item_id)
        cols = [c for c in df.columns if c != "__qdc_row_id"]
        return type_analysis.assess_types(df, markers_param(None), cols)
    return _cached_assess(project, item_id, "types", _run)


@router.get("/assess/normalization")
def assess_normalization(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    return _cached_assess(project, item_id, "normalization",
                          lambda: normalize.assess_normalization(_working_df(project, item_id)))


@router.get("/assess/basic")
def assess_basic(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    return _cached_assess(project, item_id, "basic",
                          lambda: basic.assess_basic(_working_df(project, item_id)))


class InvalidRuleRequest(BaseModel):
    column: str
    rule: dict


@router.post("/assess/invalid")
def assess_invalid(item_id: str, body: InvalidRuleRequest, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = _working_df(project, item_id)
    if body.column not in df.columns:
        raise HTTPException(status_code=404, detail="Column not found")
    try:
        return basic.eval_invalid_rule(df, body.column, body.rule)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class GroupImputeRequest(BaseModel):
    column: str
    group_by: list[str]


@router.post("/assess/group-impute")
def assess_group_impute(item_id: str, body: GroupImputeRequest, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = _working_df(project, item_id)
    return missingness.unimputable_groups(df, body.column, body.group_by, markers_param(None))


# ---------------------------------------------------------------------------
@router.get("/missing-rows")
def missing_rows(item_id: str, column: str, n: int = 20, session: Session = Depends(require_session)):
    """Rows where the given column holds a recognized missing value."""
    project, _ = _project_item(session, item_id)
    df = _working_df(project, item_id)
    if column not in df.columns:
        raise HTTPException(status_code=404, detail=f"Unknown column: {column}")
    hit = df.filter(missing_expr(column, markers_param(None)))
    return {
        "missing_count": hit.height,
        "rows": hit.head(min(max(n, 1), 50)).to_dicts(),
    }


# Op preview (before/after without applying)
# ---------------------------------------------------------------------------


class OpBody(BaseModel):
    op_type: str
    stage: str
    params: dict = {}
    target_columns: list[str] = []


@router.post("/ops/preview")
def preview_op(item_id: str, body: OpBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    if body.op_type not in OP_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown operation type: {body.op_type}")
    df = _working_df(project, item_id)
    spec = OperationSpec(op_type=body.op_type, stage=body.stage, params=body.params, target_columns=body.target_columns)
    try:
        after = execute_op(df, spec)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Operation failed: {exc}")

    changed_ids: list[int] = []
    if "__qdc_row_id" in after.columns and "__qdc_row_id" in df.columns:
        joined = df.join(after, on="__qdc_row_id", how="inner", suffix="__after")
        shared = [c for c in df.columns if c in after.columns and c != "__qdc_row_id"]
        diff_exprs = [pl.col(c).ne_missing(pl.col(f"{c}__after")) for c in shared]
        if diff_exprs:
            changed_ids = (
                joined.filter(pl.any_horizontal(diff_exprs))["__qdc_row_id"].to_list()
            )
    sample_ids = changed_ids[:10]
    before_sample = df.filter(pl.col("__qdc_row_id").is_in(sample_ids)).to_dicts() if sample_ids else []
    after_sample = after.filter(pl.col("__qdc_row_id").is_in(sample_ids)).to_dicts() if sample_ids else []
    return {
        "affected_rows": len(changed_ids),
        "row_count_before": df.height,
        "row_count_after": after.height,
        "columns_before": [c for c in df.columns if c != "__qdc_row_id"],
        "columns_after": [c for c in after.columns if c != "__qdc_row_id"],
        "before_sample": before_sample,
        "after_sample": after_sample,
    }


# ---------------------------------------------------------------------------
# Duplicate detection (job-backed)
# ---------------------------------------------------------------------------


class DupConfig(BaseModel):
    columns: list[str]
    blocking_columns: list[str] = []
    threshold: int = 100
    tag: str | None = None


def _dup_cfg_valid(df: pl.DataFrame, cfg: DupConfig) -> None:
    if not cfg.columns:
        raise HTTPException(status_code=400, detail="Select at least one comparison column")
    if cfg.threshold not in (80, 90, 100):
        raise HTTPException(status_code=400, detail="Threshold must be 80, 90, or 100")
    missing = [c for c in cfg.columns + cfg.blocking_columns if c not in df.columns or c == "__qdc_row_id"]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown columns: {missing}")


@router.post("/duplicates/estimate")
def dup_estimate(item_id: str, body: DupConfig, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = _working_df(project, item_id)
    _dup_cfg_valid(df, body)
    return estimate_candidates(df, body.columns, body.blocking_columns, body.threshold, markers_param(None))


def _run_duplicate_analysis(job: Job, session: Session, item_id: str, cfg: dict) -> dict:
    project = Project.load(session)
    df = project.history(item_id).working()
    result = analyze_duplicates(
        df, cfg["columns"], cfg.get("blocking_columns", []), cfg["threshold"], job=job
    )
    result["tag"] = cfg.get("tag")
    save_result(project.item_dir(item_id), result)
    project.set_stage_state(item_id, Stage.DUPLICATES, "in_progress")
    return result.get("summary", {})


@router.post("/duplicates/analyze", status_code=202)
def dup_analyze(item_id: str, body: DupConfig, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    df = _working_df(project, item_id)
    _dup_cfg_valid(df, body)
    job_id = job_registry.submit(
        session.token, "duplicates", _run_duplicate_analysis, session, item_id, body.model_dump()
    )
    return {"job_id": job_id}


@router.get("/duplicates/result")
def dup_result(item_id: str, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    result = load_result(project.item_dir(item_id))
    if result is None:
        raise HTTPException(status_code=404, detail="No duplicate analysis result")
    return result


class DupDecision(BaseModel):
    # records under explicit review for this decision
    row_ids: list[int]
    action: str  # keep_all | keep_first | keep_last | keep_most_complete | keep_selected | merge
    keep_row_id: int | None = None          # for keep_selected / merge primary
    field_values: dict = {}                 # for merge: explicit conflict resolutions


@router.post("/duplicates/resolve")
def dup_resolve(item_id: str, body: dict, session: Session = Depends(require_session)):
    """Apply reviewed duplicate resolutions. Only records explicitly selected
    by the user are merged or removed (revised plan)."""
    project, _ = _project_item(session, item_id)
    decisions = [DupDecision(**d) for d in body.get("decisions", [])]
    df = _working_df(project, item_id)
    markers = markers_param(None)
    all_cols = [c for c in df.columns if c != "__qdc_row_id"]
    history = project.history(item_id)
    applied: list[dict] = []

    for d in decisions:
        ids = [i for i in d.row_ids if i in set(df["__qdc_row_id"].to_list())]
        if d.action == "keep_all" or not ids:
            continue
        if d.action == "merge":
            if d.keep_row_id is None or d.keep_row_id not in ids:
                raise HTTPException(status_code=400, detail="merge requires a valid keep_row_id (primary record)")
            drops = [i for i in ids if i != d.keep_row_id]
            spec = OperationSpec(
                op_type="merge_records", stage=Stage.DUPLICATES,
                params={"primary_row_id": d.keep_row_id, "drop_row_ids": drops, "field_values": d.field_values},
                target_columns=list(d.field_values.keys()),
            )
        else:
            if d.action == "keep_first":
                keep = min(ids)
            elif d.action == "keep_last":
                keep = max(ids)
            elif d.action == "keep_most_complete":
                keep = max(ids, key=lambda r: completeness(df, r, all_cols, markers))
            elif d.action == "keep_selected":
                if d.keep_row_id is None or d.keep_row_id not in ids:
                    raise HTTPException(status_code=400, detail="keep_selected requires a valid keep_row_id")
                keep = d.keep_row_id
            else:
                raise HTTPException(status_code=400, detail=f"Unknown action: {d.action}")
            drops = [i for i in ids if i != keep]
            if not drops:
                continue
            spec = OperationSpec(
                op_type="drop_rows", stage=Stage.DUPLICATES,
                params={"row_ids": drops},
            )
        df = history.apply(spec)
        applied.append(spec.to_dict())

    marked = project.invalidate_dependents(item_id, Stage.DUPLICATES)
    project.set_stage_state(item_id, Stage.DUPLICATES, "completed")
    return {
        "applied": applied,
        "invalidated": marked,
        "row_count": df.height,
        "can_undo": history.can_undo(),
        "can_redo": history.can_redo(),
    }


# ---------------------------------------------------------------------------
# Stage + item status management
# ---------------------------------------------------------------------------


class StageBody(BaseModel):
    stage: str
    state: str


@router.post("/stage")
def set_stage(item_id: str, body: StageBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    if body.stage not in ALL_STAGES:
        raise HTTPException(status_code=400, detail=f"Unknown stage: {body.stage}")
    project.set_stage_state(item_id, body.stage, body.state)
    return {"stage_states": project.item_meta(item_id)["stage_states"]}


class StatusBody(BaseModel):
    status: str


@router.post("/status")
def set_status(item_id: str, body: StatusBody, session: Session = Depends(require_session)):
    project, _ = _project_item(session, item_id)
    project.set_item_status(item_id, body.status)
    return {"status": body.status}
