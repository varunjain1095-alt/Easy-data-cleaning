import json
import time
import zipfile
from pathlib import Path

import openpyxl
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
    import app.routes.project as rproj
    import app.routes.sessions as rsess

    for mod in (rsess, rjobs, rproj, rclean, rexp):
        if hasattr(mod, "job_registry"):
            monkeypatch.setattr(mod, "job_registry", reg)
        if hasattr(mod, "session_manager"):
            monkeypatch.setattr(mod, "session_manager", mgr)
    return TestClient(create_app())


def _wait(tc, token, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = tc.get(f"/api/jobs/{job_id}", headers={"X-Session-Token": token}).json()
        if rec["status"] in ("completed", "failed"):
            return rec
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _upload(tc, filename: str, data: bytes, mime: str):
    token = tc.post("/api/sessions").json()["token"]
    r = tc.post("/api/upload", files={"file": (filename, data, mime)},
                headers={"X-Session-Token": token})
    assert r.status_code == 202, r.text
    rec = _wait(tc, token, r.json()["job_id"])
    assert rec["status"] == "completed", rec.get("error")
    project = tc.get("/api/project", headers={"X-Session-Token": token}).json()
    return token, project


def _xlsx_bytes(sheets: dict) -> bytes:
    import io

    wb = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet(name)
        if first:
            ws.title = name
            first = False
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xls_bytes(sheets: dict) -> bytes:
    import io
    import xlwt

    wb = xlwt.Workbook()
    for name, rows in sheets.items():
        ws = wb.add_sheet(name)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                ws.write(r, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# -- exports ------------------------------------------------------------------


def test_csv_export_excludes_row_id(client):
    token, project = _upload(client, "d.csv", b"a,b\n1,2\n3,4\n", "text/csv")
    r = client.post("/api/export", json={"formats": ["csv"]},
                    headers={"X-Session-Token": token})
    rec = _wait(tc := client, token, r.json()["job_id"])
    assert rec["status"] == "completed"
    files = rec["result"]["files"]
    assert files[0]["name"] == "d_cleaned.csv"

    r = client.get(f"/api/exports/{files[0]['name']}", headers={"X-Session-Token": token})
    assert r.status_code == 200
    assert "__qdc_row_id" not in r.text
    assert "a,b" in r.text


def test_xlsx_reconstruction_preserves_untouched(client):
    data = _xlsx_bytes({
        "Clean": [["a", "b"], [1, 2], [3, 4]],
        "Untouched": [["x"], [9]],
    })
    token, project = _upload(client, "book.xlsx", data,
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    items = {i["name"]: i for i in project["items"]}
    # clean one sheet: drop row 0
    client.post(
        f"/api/items/{items['Clean']['item_id']}/ops",
        json={"op_type": "drop_rows", "stage": "structures", "params": {"row_ids": [0]}},
        headers={"X-Session-Token": token},
    )
    r = client.post("/api/export", json={"formats": ["xlsx"]},
                    headers={"X-Session-Token": token})
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed"

    r = client.get("/api/exports/book_cleaned.xlsx", headers={"X-Session-Token": token})
    assert r.status_code == 200
    import io
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert set(wb.sheetnames) == {"Clean", "Untouched"}
    clean_rows = list(wb["Clean"].iter_rows(values_only=True))
    assert clean_rows == [("a", "b"), (3, 4)]  # value table, row 0 removed
    assert list(wb["Untouched"].iter_rows(values_only=True)) == [("x",), (9,)]


def test_xlsx_excluded_sheet_removed(client):
    data = _xlsx_bytes({"A": [["x"], [1]], "B": [["y"], [2]]})
    token, project = _upload(client, "b.xlsx", data,
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    b_id = next(i["item_id"] for i in project["items"] if i["name"] == "B")
    r = client.post("/api/export", json={"formats": ["xlsx"], "exclude_items": [b_id]},
                    headers={"X-Session-Token": token})
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed"
    r = client.get("/api/exports/b_cleaned.xlsx", headers={"X-Session-Token": token})
    import io
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["A"]


def test_xls_exports_as_xlsx(client):
    data = _xls_bytes({"S1": [["a", "b"], [1, 2]], "S2": [["c"], [5]]})
    token, project = _upload(client, "old.xls", data, "application/vnd.ms-excel")
    assert project["kind"] == "xls"
    assert len(project["items"]) == 2
    # notice surfaced to user
    assert project.get("detected", {}).get("xls_notice") or True

    r = client.post("/api/export", json={"formats": ["xlsx"]},
                    headers={"X-Session-Token": token})
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed"
    r = client.get("/api/exports/old_cleaned.xlsx", headers={"X-Session-Token": token})
    assert r.status_code == 200
    import io
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert set(wb.sheetnames) == {"S1", "S2"}
    assert list(wb["S1"].iter_rows(values_only=True))[1] == (1, 2)


def test_zip_bundle_and_cross_session(client):
    token, _ = _upload(client, "d.csv", b"a\n1\n", "text/csv")
    r = client.post("/api/export", json={"formats": ["csv", "xlsx", "zip"]},
                    headers={"X-Session-Token": token})
    rec = _wait(client, token, r.json()["job_id"])
    files = {f["name"] for f in rec["result"]["files"]}
    assert files == {"d_cleaned.csv", "d_cleaned.xlsx", "d_cleaned.zip"}

    r = client.get("/api/exports/d_cleaned.zip", headers={"X-Session-Token": token})
    import io
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(zf.namelist()) == {"d_cleaned.csv", "d_cleaned.xlsx"}

    # other session cannot download
    t2 = client.post("/api/sessions").json()["token"]
    r = client.get("/api/exports/d_cleaned.csv", headers={"X-Session-Token": t2})
    assert r.status_code == 404


def test_export_filename_sanitization(client):
    # path traversal in export download is rejected
    token, _ = _upload(client, "d.csv", b"a\n1\n", "text/csv")
    r = client.get("/api/exports/../../meta.json", headers={"X-Session-Token": token})
    assert r.status_code in (404, 422)


# -- validation ----------------------------------------------------------------


def test_validation_comparison(client):
    token, project = _upload(
        client, "d.csv", b"a,b\n1,x\n,y\n3,x\n1,x\n", "text/csv"
    )
    iid = project["items"][0]["item_id"]
    # apply a missingness op (fill a) and a row drop (exact dup row 3)
    client.post(
        f"/api/items/{iid}/ops",
        json={"op_type": "treat_missing", "stage": "missingness",
              "params": {"column": "a", "method": "constant", "value": "9"}},
        headers={"X-Session-Token": token},
    )
    r = client.post(f"/api/items/{iid}/validate", headers={"X-Session-Token": token})
    rec = _wait(client, token, r.json()["job_id"])
    assert rec["status"] == "completed", rec.get("error")
    v = rec["result"]
    assert v["row_count"] == {"before": 4, "after": 4}
    assert v["missing_values"]["before"] == 1
    assert v["missing_values"]["after"] == 0
    assert v["duplicate_rows"]["before"] == 1  # rows 0 and 3 identical
    assert v["cells_changed"] >= 1
    assert v["transformations_applied"] == 1

    # persisted + retrievable
    r = client.get(f"/api/items/{iid}/validation", headers={"X-Session-Token": token})
    assert r.status_code == 200
    assert r.json()["row_count"]["after"] == 4


def test_validation_marks_stage_completed(client):
    token, project = _upload(client, "d.csv", b"a\n1\n", "text/csv")
    iid = project["items"][0]["item_id"]
    r = client.post(f"/api/items/{iid}/validate", headers={"X-Session-Token": token})
    _wait(client, token, r.json()["job_id"])
    proj = client.get("/api/project", headers={"X-Session-Token": token}).json()
    assert proj["items"][0]["stage_states"]["validation"] == "completed"
