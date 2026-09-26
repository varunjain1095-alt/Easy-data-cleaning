from fastapi import APIRouter, Depends

from ..jsonsafe import SafeJSONResponse
from ..security import require_session
from ..sessions import Session, session_manager

router = APIRouter(prefix="/api", tags=["sessions"], default_response_class=SafeJSONResponse)


@router.post("/sessions")
def create_session():
    session = session_manager.create()
    return {"token": session.token, "expires_at": session.expires_at}


@router.get("/session")
def session_info(session: Session = Depends(require_session)):
    return {
        "token": session.token,
        "expires_at": session.expires_at,
        "created_at": session.meta["created_at"],
    }
