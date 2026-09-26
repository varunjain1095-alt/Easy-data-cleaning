import time

import polars as pl
import pytest
from fastapi.testclient import TestClient

import app.jobs as jobs_mod
import app.outliers as outliers
import app.sessions as sessions_mod
from app.config import ROW_ID_COLUMN
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
        if hasattr(mod, "job_registry"):
            monkeypatch.setattr(mod, "job_registry", reg)
        if hasattr(mod, "session_manager"):
            monkeypatch.setattr(mod, "session_manager", mgr)
    return TestClient(create_app())


def _upload(tc, csv: bytes):
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
    return token, tc.get("/api/project", headers={"X-Session-Token": token}).json()["items"][0]["item_id"]


def _df(**cols):
    n = len(next(iter(cols.values())))
    return pl.DataFrame({ROW_ID_COLUMN: list(range(n)), **cols})


# -- univariate screening ------------------------------------------------------


def test_univariate_iqr_defaults():
    df = _df(v=[10.0] * 20 + [500.0])
    res = outliers.univariate_screen(df, "v", "iqr")
    assert res["stats"]["median"] == 10.0
    assert res["outlier_count"] == 1
    assert res["flagged"][0]["row_id"] == 20
    assert res["treatments_enabled"] is False
    assert len(res["histogram"]) > 0  # full-data bins


def test_univariate_methods():
    df = _df(v=list(range(1, 50)) + [1000])
    for m, kw in [("zscore", {}), ("modified_zscore", {}), ("percentile", {"lo": 2, "hi": 98}), ("manual", {"min": 0, "max": 100})]:
        res = outliers.univariate_screen(df, "v", m, kw)
        assert res["outlier_count"] >= 1, m


def test_univariate_nonnumeric_rejected():
    df = _df(t=["a", "b", "c"])
    with pytest.raises(ValueError):
        outliers.univariate_screen(df, "t", "iqr")


# -- context recommendations -----------------------------------------------------


def test_context_exclusions():
    df = _df(
        target=[1.0] * 20 + [999.0],
        uid=list(range(21)),                        # identifier
        const=[7] * 21,                             # constant
        freetext=[f"some long free text number {i} describing things" for i in range(21)],
        mostly_missing=[None] * 20 + [1.0],         # >40% missing
        group=["g1"] * 10 + ["g2"] * 11,            # usable categorical
        other=[float(i % 5) for i in range(21)],    # usable numeric (repeated vals)
    )
    cands = outliers.recommend_context(df, "target", sorted({"na", "null", ""}))
    names = [c["column"] for c in cands]
    assert "uid" not in names and "const" not in names
    assert "freetext" not in names and "mostly_missing" not in names
    assert "group" in names and "other" in names


def test_outcome_recommended_first():
    df = _df(target=[1.0] * 20 + [999.0], churn=[0] * 10 + [1] * 11)
    cands = outliers.recommend_context(df, "target", [], outcome={"column": "churn"})
    assert cands[0]["column"] == "churn"
    assert cands[0].get("is_outcome") is True


# -- bivariate evidence ------------------------------------------------------------


def test_bivariate_numeric_explained_vs_unusual():
    # y = 2x; row 20: x=21,y=200 (off trend); row 21: x=100,y=200 (on trend, extreme)
    xs = list(range(1, 21)) + [21, 100]
    ys = [2 * x for x in xs[:-2]] + [200, 200]
    df = _df(x=[float(v) for v in xs], y=[float(v) for v in ys])
    res = outliers.univariate_screen(df, "y", "iqr")
    flagged = [f["row_id"] for f in res["flagged"]]
    assert set(flagged) == {20, 21}
    ev = outliers.bivariate_review(df, "y", "x", flagged, [])
    assert ev["kind"] == "numeric"
    assert ev["evidence"]["20"] == "still_unusual"   # off the y~2x line
    assert ev["evidence"]["21"] == "explained_by_context"  # extreme but on the line
    assert ev["correlation"] > 0.5


def test_bivariate_categorical_group_position():
    df = _df(
        salary=[50.0] * 10 + [200.0] * 10 + [65.0],
        level=["junior"] * 10 + ["senior"] * 10 + ["senior"],
    )
    # row 20: senior with 65 -> unusual globally? med=... check group-position
    res = outliers.univariate_screen(df, "salary", "iqr")
    ev = outliers.bivariate_review(df, "salary", "level", [f["row_id"] for f in res["flagged"]], [])
    assert ev["kind"] == "categorical"
    assert "groups" in ev


def test_bivariate_insufficient_evidence():
    df = _df(a=[1.0, 2.0, 999.0], b=[1.0, 2.0, 3.0])
    ev = outliers.bivariate_review(df, "a", "b", [2], [])
    assert ev["insufficient_evidence"] is True
    assert ev["evidence"]["2"] in ("insufficient_evidence", "still_unusual", "explained_by_context")


# -- API gating -------------------------------------------------------------------


def test_treatment_gated_until_review(client):
    csv = b"x,y\n" + b"\n".join(f"{i},{2*i}".encode() for i in range(1, 21)) + b"\n21,200\n100,200\n"
    token, iid = _upload(client, csv)

    # screen y
    r = client.get(f"/api/items/{iid}/outliers/screen/y?method=iqr",
                   headers={"X-Session-Token": token})
    assert r.status_code == 200
    flagged = [f["row_id"] for f in r.json()["flagged"]]

    # treatment before bivariate review -> 409
    r = client.post(
        f"/api/items/{iid}/outliers/treat",
        json={"column": "y", "row_ids": flagged, "action": "remove"},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 409

    # bivariate review
    r = client.post(
        f"/api/items/{iid}/outliers/bivariate",
        json={"column": "y", "context": "x"},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    ev = r.json()["evidence"]

    # classify one record
    rid = flagged[0]
    r = client.post(
        f"/api/items/{iid}/outliers/classify",
        json={"column": "y", "row_id": rid, "classification": "likely_error"},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200

    # now removal works
    r = client.post(
        f"/api/items/{iid}/outliers/treat",
        json={"column": "y", "row_ids": [rid], "action": "remove"},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    assert r.json()["distribution"]["before"]["max"] == 200


def test_outcome_declaration(client):
    token, iid = _upload(client, b"a,churn\n1,yes\n2,no\n")
    r = client.post(
        f"/api/items/{iid}/outcome",
        json={"column": "churn", "meaning": "customer churn", "positive_class": "yes"},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    assert r.json()["outcome"]["column"] == "churn"
    # cleared outcome keeps contextual bivariate available
    r = client.post(f"/api/items/{iid}/outcome", json={"column": None},
                    headers={"X-Session-Token": token})
    assert r.json()["outcome"] is None


def test_cap_and_impute_treatments(client):
    csv = b"v,g\n" + b"\n".join(b"10,a" for _ in range(20)) + b"\n500,a\n"
    token, iid = _upload(client, csv)
    flagged = [f["row_id"] for f in client.get(
        f"/api/items/{iid}/outliers/screen/v?method=manual&min=0&max=100",
        headers={"X-Session-Token": token}).json()["flagged"]]
    assert flagged == [20]
    # review then cap
    r = client.post(f"/api/items/{iid}/outliers/bivariate",
                    json={"column": "v", "context": "g"},
                    headers={"X-Session-Token": token})
    assert r.status_code == 200
    r = client.post(
        f"/api/items/{iid}/outliers/treat",
        json={"column": "v", "row_ids": flagged, "action": "cap", "min": 0, "max": 100},
        headers={"X-Session-Token": token},
    )
    assert r.status_code == 200
    prev = client.get(f"/api/items/{iid}/preview?n=100", headers={"X-Session-Token": token}).json()
    vals = [row["v"] for row in prev["rows"]]
    assert max(vals) == 100
