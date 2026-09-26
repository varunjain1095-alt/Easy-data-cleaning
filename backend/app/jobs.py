import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .config import JOB_WORKERS, SESSIONS_DIR
from .security import new_id
from .sessions import _atomic_write_json


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class Job:
    """Handle passed to job functions for progress reporting."""

    def __init__(self, record: dict, store_path: Path, lock: threading.Lock):
        self.record = record
        self._store_path = store_path
        self._lock = lock

    @property
    def id(self) -> str:
        return self.record["id"]

    def update_progress(self, message: str = "", done: int | None = None, total: int | None = None) -> None:
        with self._lock:
            progress = self.record.setdefault("progress", {})
            if message:
                progress["message"] = message
            if done is not None:
                progress["done"] = done
            if total is not None:
                progress["total"] = total
            self.record["updated_at"] = time.time()
            _atomic_write_json(self._store_path, self.record)


class JobRegistry:
    """In-process background job execution via a thread pool.

    Job state is persisted to the session directory so results survive page
    refreshes. On process restart, persisted queued/running jobs are marked
    'interrupted' and can be restarted by the user - never shown as completed.
    """

    def __init__(self, sessions_root: Path = SESSIONS_DIR, workers: int = JOB_WORKERS):
        self.sessions_root = sessions_root
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qdc-job")
        self._jobs: dict[str, dict] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _store_path(self, session_token: str, job_id: str) -> Path:
        return self.sessions_root / session_token / "jobs" / f"{job_id}.json"

    def _persist(self, session_token: str, record: dict) -> None:
        _atomic_write_json(self._store_path(session_token, record["id"]), record)

    def submit(self, session_token: str, kind: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        job_id = new_id()
        record = {
            "id": job_id,
            "session_token": session_token,
            "kind": kind,
            "status": JobStatus.QUEUED.value,
            "progress": {"message": "Queued", "done": 0, "total": None},
            "result": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        lock = threading.Lock()
        with self._registry_lock:
            self._jobs[job_id] = record
            self._locks[job_id] = lock
        self._persist(session_token, record)
        self._executor.submit(self._run, session_token, record, lock, fn, args, kwargs)
        return job_id

    def _run(self, session_token: str, record: dict, lock: threading.Lock, fn: Callable, args: tuple, kwargs: dict) -> None:
        job = Job(record, self._store_path(session_token, record["id"]), lock)
        try:
            with lock:
                record["status"] = JobStatus.RUNNING.value
                record["updated_at"] = time.time()
                self._persist(session_token, record)
            result = fn(job, *args, **kwargs)
            with lock:
                record["status"] = JobStatus.COMPLETED.value
                record["result"] = result
                record["updated_at"] = time.time()
                self._persist(session_token, record)
        except Exception as exc:  # noqa: BLE001 - job failures are captured, not raised
            with lock:
                record["status"] = JobStatus.FAILED.value
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["traceback"] = traceback.format_exc()[-4000:]
                record["updated_at"] = time.time()
                self._persist(session_token, record)

    def get(self, session_token: str, job_id: str) -> dict | None:
        """Return the job record, or None. Jobs from other sessions return None (403-like)."""
        record = self._jobs.get(job_id)
        if record is None:
            record = self._load_persisted(session_token, job_id)
        if record is None or record.get("session_token") != session_token:
            return None
        return self._public(record)

    def _load_persisted(self, session_token: str, job_id: str) -> dict | None:
        path = self._store_path(session_token, job_id)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        with self._registry_lock:
            self._jobs[job_id] = record
            self._locks.setdefault(job_id, threading.Lock())
        return record

    def _public(self, record: dict) -> dict:
        return {k: v for k, v in record.items() if k != "traceback"}

    def recover_interrupted(self) -> int:
        """On startup, mark any persisted queued/running job as interrupted."""
        count = 0
        if not self.sessions_root.exists():
            return 0
        for jobs_dir in self.sessions_root.glob("*/jobs"):
            for path in jobs_dir.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if record.get("status") in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
                    record["status"] = JobStatus.INTERRUPTED.value
                    record["error"] = "Process restarted while job was active; safe to retry."
                    record["updated_at"] = time.time()
                    _atomic_write_json(path, record)
                    count += 1
        return count


job_registry = JobRegistry()
