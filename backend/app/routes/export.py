"""Phase 3A routes: export jobs, file downloads, validation."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..export import collect_warnings, list_exports, resolve_export, run_export
from ..jobs import job_registry
from ..jsonsafe import SafeJSONResponse
from ..project import Project
from ..security import require_session
from ..sessions import Session
from ..validation import load_validation, run_validation

router = APIRouter(prefix="/api", tags=["export"], default_response_class=SafeJSONResponse)


class ExportRequest(BaseModel):
    formats: list[str] = ["csv", "xlsx", "zip"]
    exclude_items: list[str] = []
    acknowledge_warnings: bool = False


VALID_FORMATS = {"csv", "xlsx", "zip", "report", "json", "script", "flagged"}


@router.get("/export/warnings")
def export_warnings(session: Session = Depends(require_session)):
    project = Project.load(session)
    if project is None:
        raise HTTPException(status_code=404, detail="No project in this session")
    return {"warnings": collect_warnings(project)}


@router.post("/export", status_code=202)
def start_export(body: ExportRequest, session: Session = Depends(require_session)):
    project = Project.load(session)
    if project is None:
        raise HTTPException(status_code=404, detail="No project in this session")
    valid = {i["item_id"] for i in project.data["items"]}
    unknown = [i for i in body.exclude_items if i not in valid]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown items: {unknown}")
    formats = [f for f in body.formats if f in VALID_FORMATS]
    if not formats:
        raise HTTPException(status_code=400, detail="No valid formats requested")
    warnings = collect_warnings(project)
    if warnings and not body.acknowledge_warnings:
        raise HTTPException(
            status_code=409,
            detail={"kind": "unresolved_warnings", "warnings": warnings,
                    "detail": "Acknowledge unresolved warnings to export."},
        )
    job_id = job_registry.submit(
        session.token, "export", run_export, session,
        {"formats": formats, "exclude_items": body.exclude_items,
         "acknowledge_warnings": body.acknowledge_warnings},
    )
    return {"job_id": job_id}


@router.get("/exports")
def exports(session: Session = Depends(require_session)):
    return {"files": list_exports(session)}


@router.get("/exports/{filename}")
def download(filename: str, session: Session = Depends(require_session)):
    path = resolve_export(session, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(path, filename=path.name)


@router.post("/items/{item_id}/validate", status_code=202)
def validate(item_id: str, session: Session = Depends(require_session)):
    project = Project.load(session)
    if project is None or project.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="Item not found")
    job_id = job_registry.submit(session.token, "validation", run_validation, session, item_id)
    return {"job_id": job_id}


@router.get("/items/{item_id}/validation")
def get_validation(item_id: str, session: Session = Depends(require_session)):
    project = Project.load(session)
    if project is None or project.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="Item not found")
    result = load_validation(project.item_dir(item_id))
    if result is None:
        raise HTTPException(status_code=404, detail="No validation result yet")
    return result
