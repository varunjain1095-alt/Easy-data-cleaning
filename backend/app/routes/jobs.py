from fastapi import APIRouter, Depends, HTTPException

from ..jobs import JobStatus, job_registry
from ..jsonsafe import SafeJSONResponse
from ..security import require_session
from ..sessions import Session

router = APIRouter(prefix="/api/jobs", tags=["jobs"], default_response_class=SafeJSONResponse)


@router.get("/{job_id}")
def job_status(job_id: str, session: Session = Depends(require_session)):
    record = job_registry.get(session.token, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return record


@router.post("/{job_id}/restart")
def job_restart(job_id: str, session: Session = Depends(require_session)):
    record = job_registry.get(session.token, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if record["status"] != JobStatus.INTERRUPTED.value:
        raise HTTPException(status_code=409, detail="Only interrupted jobs can be restarted")
    raise HTTPException(status_code=501, detail="Job restart not yet implemented for this job kind")
