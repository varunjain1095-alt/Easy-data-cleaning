import re
import secrets
import unicodedata

from fastapi import Depends, Header, HTTPException

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_id() -> str:
    return secrets.token_hex(16)


def sanitize_filename(name: str, fallback: str = "upload") -> str:
    """Strip path components and unsafe characters from a user-supplied filename."""
    name = unicodedata.normalize("NFKC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = _FILENAME_SAFE.sub("_", name).strip("._")
    if not name or name in {".", ".."}:
        return fallback
    return name[:180]


async def get_session_token(x_session_token: str | None = Header(default=None)) -> str:
    if not x_session_token:
        raise HTTPException(status_code=401, detail="Missing session token")
    return x_session_token


def require_session(token: str = Depends(get_session_token)):
    """Resolve and authorize the session; raises 401/403 on failure."""
    from .sessions import session_manager

    session = session_manager.get(token)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    session.touch()
    return session
