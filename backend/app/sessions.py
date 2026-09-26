import json
import os
import shutil
import threading
import time
from pathlib import Path

from .config import SESSION_TTL_SECONDS, SESSIONS_DIR
from .security import new_session_token


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically. Unique temp names avoid concurrent-writer
    collisions; os.replace retries handle Windows file-locking when the target
    is momentarily open for reading by another thread."""
    import uuid

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


# Track when each session's meta was last persisted so touch() can be debounced.
_last_touch_write: dict[str, float] = {}
_touch_lock = threading.Lock()
TOUCH_MIN_INTERVAL = 5.0  # seconds between persisted touches per session


class Session:
    def __init__(self, token: str, directory: Path, meta: dict):
        self.token = token
        self.dir = directory
        self.meta = meta

    @property
    def expires_at(self) -> float:
        return self.meta["expires_at"]

    def touch(self) -> None:
        self.meta["last_activity"] = time.time()
        self.meta["expires_at"] = time.time() + SESSION_TTL_SECONDS
        now = time.time()
        with _touch_lock:
            last = _last_touch_write.get(self.token, 0.0)
            if now - last < TOUCH_MIN_INTERVAL:
                return
            _last_touch_write[self.token] = now
        try:
            _atomic_write_json(self.dir / "meta.json", self.meta)
        except OSError:
            pass  # bookkeeping write — a transient lock must not 500 the request

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)


class SessionManager:
    """Opaque-token sessions stored as per-session directories on disk."""

    def __init__(self, root: Path = SESSIONS_DIR, ttl: int = SESSION_TTL_SECONDS):
        self.root = root
        self.ttl = ttl
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self) -> Session:
        token = new_session_token()
        directory = self.root / token
        directory.mkdir(parents=True, exist_ok=False)
        now = time.time()
        meta = {
            "created_at": now,
            "last_activity": now,
            "expires_at": now + self.ttl,
        }
        _atomic_write_json(directory / "meta.json", meta)
        return Session(token, directory, meta)

    def get(self, token: str) -> Session | None:
        if not token or "/" in token or "\\" in token or ".." in token:
            return None
        directory = self.root / token
        meta_path = directory / "meta.json"
        if not meta_path.exists():
            return None
        # A read racing a concurrent touch() write can transiently fail or see
        # partial content - retry briefly before treating the session as absent.
        meta = None
        for attempt in range(4):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                break
            except (json.JSONDecodeError, OSError):
                if attempt == 3:
                    return None
                time.sleep(0.02 * (attempt + 1))
        if time.time() > meta.get("expires_at", 0):
            self.delete(token)
            return None
        return Session(token, directory, meta)

    def delete(self, token: str) -> None:
        with self._lock:
            shutil.rmtree(self.root / token, ignore_errors=True)

    def cleanup_expired(self) -> int:
        now = time.time()
        removed = 0
        for child in self.root.iterdir():
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if now > meta.get("expires_at", 0):
                self.delete(child.name)
                removed += 1
        return removed

    def start_cleanup_loop(self, interval: int = 60) -> threading.Thread:
        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.cleanup_expired()
                except Exception:
                    pass

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread


session_manager = SessionManager()
