import json
import time

from app.jobs import JobRegistry, JobStatus


def _wait(registry, token, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = registry.get(token, job_id)
        if rec and rec["status"] in (
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
        ):
            return rec
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_job_completes_and_reports_progress(registry, session):
    def work(job):
        job.update_progress("half", done=1, total=2)
        job.update_progress("done", done=2, total=2)
        return {"ok": True}

    job_id = registry.submit(session.token, "test", work)
    rec = _wait(registry, session.token, job_id)
    assert rec["status"] == "completed"
    assert rec["result"] == {"ok": True}
    assert rec["progress"]["done"] == 2


def test_job_failure_captured(registry, session):
    def work(job):
        raise RuntimeError("boom")

    job_id = registry.submit(session.token, "test", work)
    rec = _wait(registry, session.token, job_id)
    assert rec["status"] == "failed"
    assert "boom" in rec["error"]


def test_cross_session_job_access_rejected(registry, session):
    other = session  # same session fixture, mint another token
    from app.sessions import SessionManager

    mgr = SessionManager(session.dir.parent)
    s2 = mgr.create()

    job_id = registry.submit(session.token, "test", lambda job: None)
    _wait(registry, session.token, job_id)
    assert registry.get(s2.token, job_id) is None


def test_job_status_persisted(registry, session):
    job_id = registry.submit(session.token, "test", lambda job: 42)
    _wait(registry, session.token, job_id)
    path = session.dir / "jobs" / f"{job_id}.json"
    assert path.exists()
    # The in-memory record updates before the persisted write lands; poll the file.
    deadline = time.time() + 2
    persisted = {}
    while time.time() < deadline:
        persisted = json.loads(path.read_text())
        if persisted.get("status") == "completed":
            break
        time.sleep(0.02)
    assert persisted["status"] == "completed"
    assert persisted["result"] == 42


def test_recover_interrupted(registry, session):
    jobs_dir = session.dir / "jobs"
    jobs_dir.mkdir(parents=True)
    fake = {
        "id": "abc",
        "session_token": session.token,
        "kind": "ingest",
        "status": "running",
        "progress": {},
        "result": None,
        "error": None,
        "created_at": 0,
        "updated_at": 0,
    }
    (jobs_dir / "abc.json").write_text(json.dumps(fake))
    assert registry.recover_interrupted() == 1
    rec = registry.get(session.token, "abc")
    assert rec["status"] == "interrupted"
    assert rec["result"] is None
