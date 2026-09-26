import time

import pytest
from fastapi.testclient import TestClient

import app.jobs as jobs_mod
import app.sessions as sessions_mod
from app.jobs import JobRegistry
from app.main import create_app
from app.sessions import SessionManager


@pytest.fixture
def client(tmp_path, monkeypatch):
    mgr = SessionManager(tmp_path / "sessions", ttl=3600)
    reg = JobRegistry(tmp_path / "sessions", workers=2)
    monkeypatch.setattr(sessions_mod, "session_manager", mgr)
    monkeypatch.setattr(jobs_mod, "job_registry", reg)
    # routes import the singletons by name at call time via module attrs
    import app.routes.jobs as rjobs
    import app.routes.sessions as rsess
    import app.routes.project as rproj
    import app.security as sec

    monkeypatch.setattr(rsess, "session_manager", mgr)
    monkeypatch.setattr(rjobs, "job_registry", reg)
    monkeypatch.setattr(rproj, "job_registry", reg)

    # security.require_session resolves via sessions.session_manager lazily
    monkeypatch.setattr(sec, "session_manager", mgr, raising=False)
    import app.sessions
    monkeypatch.setattr(app.sessions, "session_manager", mgr)

    app = create_app()
    return TestClient(app), mgr


def _wait_job(client, token, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}", headers={"X-Session-Token": token})
        assert r.status_code == 200
        rec = r.json()
        if rec["status"] in ("completed", "failed"):
            return rec
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_full_flow_csv(client):
    tc, _ = client
    # create session
    r = tc.post("/api/sessions")
    assert r.status_code == 200
    token = r.json()["token"]

    # unauthorized without token
    assert tc.get("/api/project").status_code == 401

    # upload csv
    csv_bytes = b"name,score\na,1\nb,\nc,3\n"
    r = tc.post(
        "/api/upload",
        files={"file": ("data.csv", csv_bytes, "text/csv")},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    rec = _wait_job(tc, token, job_id)
    assert rec["status"] == "completed", rec.get("error")

    # project + preview
    r = tc.get("/api/project", headers={"X-Session-Token": token})
    project = r.json()
    item = project["items"][0]
    assert item["row_count"] == 3

    r = tc.get(f"/api/items/{item['item_id']}/preview", headers={"X-Session-Token": token})
    assert r.status_code == 200
    assert r.json()["columns"] == [
        {"name": "name", "dtype": "String"},
        {"name": "score", "dtype": "Int64"},
    ]

    # apply an op, then undo
    r = tc.post(
        f"/api/items/{item['item_id']}/ops",
        json={
            "op_type": "fill_null_constant",
            "stage": "missingness",
            "params": {"column": "score", "value": 0},
        },
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200, r.text
    assert r.json()["can_undo"] is True

    r = tc.post(f"/api/items/{item['item_id']}/undo", headers={"X-Session-Token": token})
    assert r.status_code == 200
    r = tc.post(f"/api/items/{item['item_id']}/redo", headers={"X-Session-Token": token})
    assert r.status_code == 200


def test_oversized_upload_rejected(client, monkeypatch):
    tc, _ = client
    import app.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "MAX_UPLOAD_BYTES", 5)
    token = tc.post("/api/sessions").json()["token"]
    r = tc.post(
        "/api/upload",
        files={"file": ("data.csv", b"a,b,c,d,e,f,g,h", "text/csv")},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 413
    assert r.json()["detail"]["kind"] == "file_size"


def test_foreign_session_cannot_read_project(client):
    tc, mgr = client
    t1 = tc.post("/api/sessions").json()["token"]
    t2 = mgr.create().token
    tc.post(
        "/api/upload",
        files={"file": ("d.csv", b"a\n1\n", "text/csv")},
        headers={"X-Session-Token": t1},
    )
    r = tc.get("/api/project", headers={"X-Session-Token": t2})
    assert r.status_code == 404
