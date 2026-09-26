"""Security regression tests (architecture section 16)."""

import io
import time
import zipfile

import openpyxl
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
    import app.routes.cleaning as rclean
    import app.routes.export as rexp
    import app.routes.jobs as rjobs
    import app.routes.outliers as rout
    import app.routes.project as rproj
    import app.routes.sessions as rsess

    for mod in (rsess, rjobs, rproj, rclean, rexp, rout):
        for attr in ("job_registry", "session_manager"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, reg if attr == "job_registry" else mgr)
    return TestClient(create_app())


def _wait(tc, token, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = tc.get(f"/api/jobs/{job_id}", headers={"X-Session-Token": token}).json()
        if rec["status"] in ("completed", "failed"):
            return rec
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_upload_rate_limited(client):
    token = client.post("/api/sessions").json()["token"]
    last = None
    for i in range(12):
        last = client.post("/api/upload", files={"file": ("d.csv", b"a\n1\n", "text/csv")},
                           headers={"X-Session-Token": token})
        if last.status_code == 429:
            break
    assert last.status_code == 429
    assert last.json()["detail"]["kind"] == "rate_limited"


def test_missing_token_rejected(client):
    assert client.get("/api/project").status_code == 401
    assert client.post("/api/upload", files={"file": ("d.csv", b"a\n1\n", "text/csv")}).status_code == 401


def test_bad_extension_rejected(client):
    token = client.post("/api/sessions").json()["token"]
    r = client.post("/api/upload", files={"file": ("x.exe", b"MZ\x90", "application/x-msdownload")},
                    headers={"X-Session-Token": token})
    assert r.status_code == 422


def test_extension_signature_mismatch_rejected(client):
    # .xlsx extension but OLE2 (encrypted) signature -> protected workbook
    token = client.post("/api/sessions").json()["token"]
    ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    r = client.post("/api/upload", files={"file": ("w.xlsx", ole2, "application/vnd.ms-excel")},
                    headers={"X-Session-Token": token})
    job = _wait(client, token, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "protected" in job["error"] or "encrypted" in job["error"]


def test_macro_workbook_rejected_via_api(client):
    wb = openpyxl.Workbook()
    wb.active.append(["a"])
    buf = io.BytesIO()
    wb.save(buf)
    p = buf.getvalue()
    # inject vbaProject.bin
    src = io.BytesIO(p)
    out = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, zin.read(n))
        zout.writestr("xl/vbaProject.bin", b"fake")
    token = client.post("/api/sessions").json()["token"]
    r = client.post("/api/upload", files={"file": ("m.xlsx", out.getvalue(), "application/vnd.ms-excel")},
                    headers={"X-Session-Token": token})
    job = _wait(client, token, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "macro" in job["error"].lower() or "Macro" in job["error"]


def test_nan_values_do_not_500_preview(client):
    """NaN floats must not break JSON serialization (real-world files contain
    NaN literals; NaN is normalized to null at ingest)."""
    token = client.post("/api/sessions").json()["token"]
    r = client.post("/api/upload", files={"file": ("n.csv", b"a;b\n1;NaN\nNaN;3\n", "text/csv")},
                    headers={"X-Session-Token": token})
    job = _wait(client, token, r.json()["job_id"])
    assert job["status"] == "completed"
    iid = client.get("/api/project", headers={"X-Session-Token": token}).json()["items"][0]["item_id"]
    r = client.get(f"/api/items/{iid}/preview", headers={"X-Session-Token": token})
    assert r.status_code == 200
    assert r.json()["rows"][0]["b"] is None


def test_session_isolation(client):
    t1 = client.post("/api/sessions").json()["token"]
    t2 = client.post("/api/sessions").json()["token"]
    client.post("/api/upload", files={"file": ("d.csv", b"a\n1\n", "text/csv")},
                headers={"X-Session-Token": t1})
    time.sleep(0.5)
    # t2 cannot see t1's project or export files
    assert client.get("/api/project", headers={"X-Session-Token": t2}).status_code == 404
    assert client.get("/api/exports/d_cleaned.csv", headers={"X-Session-Token": t2}).status_code == 404
    # t2 cannot read t1's jobs
    proj = client.get("/api/project", headers={"X-Session-Token": t1})
    assert proj.status_code == 200
