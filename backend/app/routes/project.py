from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..ingest import IngestError, process_upload, read_csv, stream_to_quarantine
from ..jsonsafe import SafeJSONResponse
from ..jobs import job_registry
from ..opspec import OP_REGISTRY, OperationSpec
from ..project import Project
from ..security import require_session
from ..sessions import Session

router = APIRouter(prefix="/api", tags=["project"], default_response_class=SafeJSONResponse)

_INGEST_ERROR_STATUS = {
    "file_size": 413,
    "row_limit": 422,
    "invalid_file": 422,
    "protected_workbook": 422,
    "macro_enabled": 422,
}


def _ingest_http_error(exc: IngestError) -> HTTPException:
    status = _INGEST_ERROR_STATUS.get(exc.kind, 422)
    return HTTPException(status_code=status, detail={"kind": exc.kind, "detail": exc.detail})


@router.post("/upload", status_code=202)
async def upload(file: UploadFile = File(...), session: Session = Depends(require_session)):
    try:
        quarantined = await stream_to_quarantine(session, file.filename or "upload", file)
    except IngestError as exc:
        raise _ingest_http_error(exc)
    job_id = job_registry.submit(session.token, "ingest", process_upload, session, quarantined)
    return {"job_id": job_id, "filename": quarantined.name}


@router.get("/project")
def get_project(session: Session = Depends(require_session)):
    project = Project.load(session)
    if project is None:
        raise HTTPException(status_code=404, detail="No project in this session")
    return project.to_dict()


@router.get("/items/{item_id}/preview")
def item_preview(item_id: str, n: int = 20, session: Session = Depends(require_session)):
    project = Project.load(session)
    if project is None:
        raise HTTPException(status_code=404, detail="No project in this session")
    preview = project.preview(item_id, n=min(n, 500))
    if preview is None:
        raise HTTPException(status_code=404, detail="Item not found or not loaded")
    return preview


class ReparseRequest(BaseModel):
    encoding: str | None = None
    delimiter: str | None = None


@router.post("/items/{item_id}/reparse")
def reparse_item(item_id: str, body: ReparseRequest, session: Session = Depends(require_session)):
    """Re-parse a CSV with user-corrected encoding/delimiter (preview correction)."""
    project = Project.load(session)
    if project is None or project.data["kind"] != "csv":
        raise HTTPException(status_code=400, detail="Reparse is only available for CSV uploads")
    item = project.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    history = project.history(item_id)
    if history.ops:
        raise HTTPException(status_code=409, detail="Cannot reparse after transformations have been applied")
    source = project.dir / "source.csv"
    if not source.exists():
        source = project.dir / "source.txt"
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source file missing")

    detected = project.data.get("detected", {}).get("csv_settings", {})
    encoding = body.encoding or detected.get("encoding") or "utf-8"
    delimiter = body.delimiter or detected.get("delimiter") or ","
    try:
        df = read_csv(source, encoding, delimiter)
    except IngestError as exc:
        raise _ingest_http_error(exc)
    history.initialize(df)
    project.set_item_dims(item_id, df.height, df.width - 1)
    return project.preview(item_id)


class OpRequest(BaseModel):
    op_type: str
    stage: str
    params: dict = {}
    target_columns: list[str] = []


@router.post("/items/{item_id}/ops")
def apply_op(item_id: str, body: OpRequest, session: Session = Depends(require_session)):
    project = _get_project_item(session, item_id)
    if body.op_type not in OP_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown operation type: {body.op_type}")
    spec = OperationSpec(
        op_type=body.op_type,
        stage=body.stage,
        params=body.params,
        target_columns=body.target_columns,
    )
    history = project.history(item_id)
    try:
        df = history.apply(spec)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Operation failed: {exc}")
    marked = project.invalidate_dependents(item_id, body.stage)
    _touch_item_in_progress(project, item_id)
    meta = project.item_meta(item_id)
    if meta["stage_states"].get(body.stage) == "not_started":
        project.set_stage_state(item_id, body.stage, "in_progress")
    return {
        "applied": spec.to_dict(),
        "invalidated": marked,
        "row_count": df.height,
        "column_count": df.width - 1,
        "can_undo": history.can_undo(),
        "can_redo": history.can_redo(),
    }


@router.post("/items/{item_id}/undo")
def undo(item_id: str, session: Session = Depends(require_session)):
    project = _get_project_item(session, item_id)
    history = project.history(item_id)
    try:
        df = history.undo()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Undo failed during replay: {exc}")
    if df is None:
        raise HTTPException(status_code=409, detail="Nothing to undo")
    return {"row_count": df.height, "pointer": history.pointer, "can_undo": history.can_undo(), "can_redo": history.can_redo()}


@router.post("/items/{item_id}/redo")
def redo(item_id: str, session: Session = Depends(require_session)):
    project = _get_project_item(session, item_id)
    history = project.history(item_id)
    try:
        df = history.redo()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Redo failed during replay: {exc}")
    if df is None:
        raise HTTPException(status_code=409, detail="Nothing to redo")
    return {"row_count": df.height, "pointer": history.pointer, "can_undo": history.can_undo(), "can_redo": history.can_redo()}


@router.get("/items/{item_id}/history")
def get_history(item_id: str, session: Session = Depends(require_session)):
    project = _get_project_item(session, item_id)
    return project.history(item_id).to_dict()


def _get_project_item(session: Session, item_id: str) -> Project:
    project = Project.load(session)
    if project is None:
        raise HTTPException(status_code=404, detail="No project in this session")
    if project.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return project


def _touch_item_in_progress(project: Project, item_id: str) -> None:
    item = project.get_item(item_id)
    if item and item["status"] == "not_started":
        project.set_item_status(item_id, "in_progress")
