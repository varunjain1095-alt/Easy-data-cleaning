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
    import app.routes.cleaning as rclean
    import app.routes.jobs as rjobs
    import app.routes.project as rproj
    import app.routes.sessions as rsess
    import app.security as sec

    monkeypatch.setattr(rsess, "session_manager", mgr)
    monkeypatch.setattr(rjobs, "job_registry", reg)
    monkeypatch.setattr(rproj, "job_registry", reg)
    monkeypatch.setattr(rclean, "job_registry", reg)
    return TestClient(create_app())


def _setup(tc, csv: bytes):
    token = tc.post("/api/sessions").json()["token"]
    r = tc.post("/api/upload", files={"file": ("d.csv", csv, "text/csv")},
                headers={"X-Session-Token": token})
    job_id = r.json()["job_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        rec = tc.get(f"/api/jobs/{job_id}", headers={"X-Session-Token": token}).json()
        if rec["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert rec["status"] == "completed", rec.get("error")
    item = tc.get("/api/project", headers={"X-Session-Token": token}).json()["items"][0]
    return token, item["item_id"]


def test_assessments_end_to_end(client):
    token, iid = _setup(client, b"name,age,gender\nAlice,30,F\nBob,,male\n,25,M\n")
    for stage in ("missingness", "types", "normalization", "basic"):
        r = client.get(f"/api/items/{iid}/assess/{stage}", headers={"X-Session-Token": token})
        assert r.status_code == 200, (stage, r.text)
    miss = client.get(f"/api/items/{iid}/assess/missingness", headers={"X-Session-Token": token}).json()
    assert {c["column"] for c in miss["columns"]} == {"name", "age", "gender"}


def test_op_preview_and_apply_and_undo(client):
    token, iid = _setup(client, b"a,b\n  x ,1\ny,2\n")
    op = {"op_type": "normalize", "stage": "normalization",
          "params": {"columns": ["a"], "operations": [{"op": "trim"}]},
          "target_columns": ["a"]}
    r = client.post(f"/api/items/{iid}/ops/preview", json=op, headers={"X-Session-Token": token})
    assert r.status_code == 200
    assert r.json()["affected_rows"] >= 1

    r = client.post(f"/api/items/{iid}/ops", json=op, headers={"X-Session-Token": token})
    assert r.status_code == 200
    # normalization invalidates missingness state? it was not_started; item now in_progress
    proj = client.get("/api/project", headers={"X-Session-Token": token}).json()
    assert proj["items"][0]["status"] == "in_progress"
    assert proj["items"][0]["stage_states"]["normalization"] == "in_progress"

    r = client.post(f"/api/items/{iid}/undo", headers={"X-Session-Token": token})
    assert r.status_code == 200


def test_invalid_rule_eval_and_treat(client):
    token, iid = _setup(client, b"age\n25\n-3\n40\n999\n")
    r = client.post(
        f"/api/items/{iid}/assess/invalid",
        json={"column": "age", "rule": {"type": "range", "min": 0, "max": 120}},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    assert r.json()["invalid_count"] == 2

    r = client.post(
        f"/api/items/{iid}/ops",
        json={"op_type": "mask_values", "stage": "invalid_values",
              "params": {"column": "age", "rule": {"type": "range", "min": 0, "max": 120}}},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    prev = client.get(f"/api/items/{iid}/preview", headers={"X-Session-Token": token}).json()
    vals = [r["age"] for r in prev["rows"]]
    assert None in vals


def test_duplicate_flow_via_api(client):
    csv = b"a,b,c\nx,1,p\nx,1,p\ny,2,q\nx,1,r\n"
    token, iid = _setup(client, csv)

    # estimate
    r = client.post(
        f"/api/items/{iid}/duplicates/estimate",
        json={"columns": ["a", "b"], "blocking_columns": [], "threshold": 100},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    est = r.json()
    assert est["candidate_pairs"] == 3  # a=x group of 3 -> C(3,2)

    # analyze (job)
    r = client.post(
        f"/api/items/{iid}/duplicates/analyze",
        json={"columns": ["a", "b"], "blocking_columns": [], "threshold": 100},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        rec = client.get(f"/api/jobs/{job_id}", headers={"X-Session-Token": token}).json()
        if rec["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert rec["status"] == "completed"

    res = client.get(f"/api/items/{iid}/duplicates/result", headers={"X-Session-Token": token}).json()
    assert res["mode"] == "exact"
    assert res["summary"]["duplicate_groups"] == 1
    members = res["groups"][0]["members"]

    # resolve: keep first
    r = client.post(
        f"/api/items/{iid}/duplicates/resolve",
        json={"decisions": [{"row_ids": members, "action": "keep_first"}]},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    # 4 rows: rows {0,1,4} identical on (a,b); keep_first keeps row 0, drops 1+4 -> 2 remain
    assert r.json()["row_count"] == 2

    # cross-session rejection on result
    t2 = client.post("/api/sessions").json()["token"]
    r = client.get(f"/api/items/{iid}/duplicates/result", headers={"X-Session-Token": t2})
    assert r.status_code == 404


def test_stage_state_transitions(client):
    token, iid = _setup(client, b"a\n1\n")
    r = client.post(f"/api/items/{iid}/stage", json={"stage": "missingness", "state": "skipped"},
                    headers={"X-Session-Token": token})
    assert r.status_code == 200
    assert r.json()["stage_states"]["missingness"] == "skipped"
    r = client.post(f"/api/items/{iid}/stage", json={"stage": "bogus", "state": "skipped"},
                    headers={"X-Session-Token": token})
    assert r.status_code == 400
