import json
import subprocess
import sys
import time
import zipfile
import io

import polars as pl
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


def _wait(tc, token, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = tc.get(f"/api/jobs/{job_id}", headers={"X-Session-Token": token}).json()
        if rec["status"] in ("completed", "failed"):
            return rec
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _upload(tc, csv: bytes):
    token = tc.post("/api/sessions").json()["token"]
    r = tc.post("/api/upload", files={"file": ("data.csv", csv, "text/csv")},
                headers={"X-Session-Token": token})
    rec = _wait(tc, token, r.json()["job_id"])
    assert rec["status"] == "completed", rec.get("error")
    return token, tc.get("/api/project", headers={"X-Session-Token": token}).json()["items"][0]["item_id"]


def _export(tc, token, formats, ack=False):
    r = tc.post("/api/export",
                json={"formats": formats, "acknowledge_warnings": ack},
                headers={"X-Session-Token": token})
    return r


# -- formula injection -----------------------------------------------------------


def test_csv_formula_injection_escaped(client):
    token, _ = _upload(client, b'a,b\n"=cmd|/c calc",1\n"+SUM(1)",2\nnormal,3\n')
    r = _export(client, token, ["csv"])
    assert r.status_code == 202
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed"
    body = client.get("/api/exports/data_cleaned.csv",
                      headers={"X-Session-Token": token}).text
    assert "'=cmd" in body and "'+SUM" in body
    assert "\nnormal,3" in body


# -- report / json / script --------------------------------------------------------


def test_report_and_json_export(client):
    token, iid = _upload(client, b"a,b\n  x ,1\ny,2\n")
    client.post(f"/api/items/{iid}/ops",
                json={"op_type": "normalize", "stage": "normalization",
                      "params": {"columns": ["a"], "operations": [{"op": "trim"}]}},
                headers={"X-Session-Token": token})
    r = _export(client, token, ["report", "json"])
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed", rec.get("error")
    names = {f["name"] for f in rec["result"]["files"]}
    assert "data_report.html" in names and "data_transformations.json" in names

    html = client.get("/api/exports/data_report.html", headers={"X-Session-Token": token}).text
    assert "Transformation history" in html
    assert "normalize" in html
    assert "Before / after" in html

    cfg = client.get("/api/exports/data_transformations.json",
                     headers={"X-Session-Token": token}).json()
    assert cfg["items"][0]["operations"][0]["op_type"] == "normalize"


def test_repro_script_replays_ops(client, tmp_path):
    csv = b"name,score\n  Alice ,10\nBob,\nCara,30\nDup,5\nDup,5\n"
    token, iid = _upload(client, csv)
    client.post(f"/api/items/{iid}/ops",
                json={"op_type": "normalize", "stage": "normalization",
                      "params": {"columns": ["name"], "operations": [{"op": "trim"}]}},
                headers={"X-Session-Token": token})
    client.post(f"/api/items/{iid}/ops",
                json={"op_type": "treat_missing", "stage": "missingness",
                      "params": {"column": "score", "method": "constant", "value": 0}},
                headers={"X-Session-Token": token})
    client.post(f"/api/items/{iid}/ops",
                json={"op_type": "drop_rows", "stage": "duplicates", "params": {"row_ids": [4]}},
                headers={"X-Session-Token": token})

    r = _export(client, token, ["script"])
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed", rec.get("error")
    script = client.get("/api/exports/data_clean.py", headers={"X-Session-Token": token}).text
    assert "pl.read_csv(INPUT)" in script
    assert "assert df.height" in script

    # run the generated script against the raw source and compare to working data
    src = tmp_path / "input.csv"
    src.write_bytes(csv)
    out = tmp_path / "out.csv"
    proc = subprocess.run(
        [sys.executable, "-c", script, str(src), str(out)],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    result = pl.read_csv(out)
    assert result.height == 4
    assert result["name"].to_list()[:3] == ["Alice", "Bob", "Cara"]
    assert result["score"].to_list() == [10, 0, 30, 5]


# -- warnings gate ------------------------------------------------------------------


def test_warnings_gate_blocks_and_acknowledges(client):
    token, iid = _upload(client, b"a\n1\n2\n")
    # complete validation, then apply an op that invalidates it
    r = client.post(f"/api/items/{iid}/validate", headers={"X-Session-Token": token})
    _wait(client, token, r.json()["job_id"])
    client.post(f"/api/items/{iid}/ops",
                json={"op_type": "drop_rows", "stage": "missingness", "params": {"row_ids": [0]}},
                headers={"X-Session-Token": token})

    w = client.get("/api/export/warnings", headers={"X-Session-Token": token}).json()
    assert any("needs_recalculation" in x for x in w["warnings"])

    r = _export(client, token, ["csv"])
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "unresolved_warnings"

    r = _export(client, token, ["csv"], ack=True)
    assert r.status_code == 202
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed"
    assert rec["result"]["acknowledged_warnings"] is True


# -- flagged records export ----------------------------------------------------------


def test_flagged_records_export(client):
    csv = b"v,g\n" + b"\n".join(b"10,a" for _ in range(15)) + b"\n999,b\n"
    token, iid = _upload(client, csv)
    client.get(f"/api/items/{iid}/outliers/screen/v?method=manual&min=0&max=100",
               headers={"X-Session-Token": token})
    # unresolved flagged outlier must be acknowledged before export
    r = _export(client, token, ["flagged"])
    assert r.status_code == 409
    r = _export(client, token, ["flagged"], ack=True)
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed", rec.get("error")
    flagged = [f for f in rec["result"]["files"] if f["kind"] == "flagged"]
    assert len(flagged) == 1
    body = client.get(f"/api/exports/{flagged[0]['name']}", headers={"X-Session-Token": token}).text
    assert "999" in body and "outlier:v" in body


def test_full_zip_bundle(client):
    token, iid = _upload(client, b"a\n1\n2\n3\n")
    client.post(f"/api/items/{iid}/ops",
                json={"op_type": "rename_column", "stage": "column_names",
                      "params": {"from": "a", "to": "b"}},
                headers={"X-Session-Token": token})
    r = _export(client, token, ["csv", "xlsx", "report", "json", "script", "zip"])
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed", rec.get("error")
    names = {f["name"] for f in rec["result"]["files"]}
    assert {"data_cleaned.csv", "data_cleaned.xlsx", "data_report.html",
            "data_transformations.json", "data_clean.py", "data_cleaned.zip"} <= names
    body = client.get("/api/exports/data_cleaned.zip", headers={"X-Session-Token": token}).content
    zf = zipfile.ZipFile(io.BytesIO(body))
    assert "data_cleaned.csv" in zf.namelist()
    assert "data_clean.py" in zf.namelist()
