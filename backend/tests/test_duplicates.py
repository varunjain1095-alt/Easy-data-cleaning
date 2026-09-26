import polars as pl
import pytest

from app import duplicates as dup
from app.config import MISSING_MARKERS, ROW_ID_COLUMN
from app.opspec import OperationSpec, Stage, execute_op

MARKERS = sorted(MISSING_MARKERS)


def _df(**cols):
    n = len(next(iter(cols.values())))
    return pl.DataFrame({ROW_ID_COLUMN: list(range(n)), **cols})


# -- exact duplicates (100%) ------------------------------------------------------


def test_exact_groups_via_hash():
    df = _df(a=["x", "y", "x", "z"], b=[1, 2, 1, 3])
    res = dup.analyze_duplicates(df, ["a", "b"], [], 100)
    assert res["mode"] == "exact"
    assert len(res["groups"]) == 1
    assert sorted(res["groups"][0]["members"]) == [0, 2]
    assert res["summary"]["duplicate_groups"] == 1
    assert res["summary"]["proposed_removals"] == 1


def test_exact_missing_matches_missing():
    df = _df(a=["x", "x", "x"], b=[None, "N/A", None])
    res = dup.analyze_duplicates(df, ["a", "b"], [], 100)
    # rows 0 and 2 both missing in b; row 1 "N/A" normalizes to missing -> all three match
    assert len(res["groups"]) == 1
    assert res["groups"][0]["members"] == [0, 1, 2]


# -- near duplicates (80/90%) -----------------------------------------------------


def test_non_transitive_chain_not_grouped():
    """A~B and B~C but NOT A~C: must not form an equivalence group."""
    # 5 columns, 90% threshold -> need >=4.5 -> 5 matching fields? use 80%: need 4/5.
    # A: shares 4 fields with B. C: shares 4 fields with B. A vs C: 3 shared.
    df = _df(
        c1=["same", "same", "diff"],
        c2=["same", "same", "same"],
        c3=["same", "same", "same"],
        c4=["same", "same", "same"],
        c5=["x", "y", "z"],
    )
    # A(0) vs B(1): c1..c4 same? A c1=same B c1=same yes; c5 x vs y differ -> 4/5=80 ✓
    # B(1) vs C(2): c1 same vs diff -> differ; c2-c4 same; c5 y vs z differ -> 3/5=60 ✗

    df = _df(
        c1=["same", "same", "same"],
        c2=["same", "same", "same"],
        c3=["same", "same", "diff"],
        c4=["same", "same", "same"],
        c5=["x", "y", "same"],
    )
    # A vs B: c1,c2,c3,c4 same; c5 differ -> 4/5=80 ✓
    # B vs C: c1,c2,c4 same; c3 differ; c5 differ -> 3/5=60 ✗
    # A vs C: c1,c2,c4 same; c3,c5 differ -> 60 ✗
    res = dup.analyze_duplicates(df, ["c1", "c2", "c3", "c4", "c5"], [], 80)
    reps = {c["representative_row_id"] for c in res["clusters"]}
    # only A-B qualifies: representative anchored
    assert len(res["clusters"]) >= 1
    for cluster in res["clusters"]:
        rep = cluster["representative_row_id"]
        for m in cluster["members"]:
            assert m["score_pct"] >= 80
            # member scored against representative only
        assert "not guaranteed" in cluster["note"]

    # Now the true non-transitive case: A-B match, B-C match, A-C does not.
    df2 = _df(
        c1=["same", "same", "diff"],
        c2=["same", "same", "same"],
        c3=["same", "same", "same"],
        c4=["same", "same", "same"],
        c5=["x", "y", "y"],
    )
    # A vs B: c1-c4 same, c5 x vs y -> 4/5 = 80 ✓
    # B vs C: c2-c4 same, c5 y vs y same; c1 same vs diff -> 4/5 = 80 ✓
    # A vs C: c1 differ, c5 differ -> 3/5 = 60 ✗
    res2 = dup.analyze_duplicates(df2, ["c1", "c2", "c3", "c4", "c5"], [], 80)
    # No cluster may assert A matches C. Representative-anchored clusters may
    # contain A and C only if both individually match the representative.
    for cluster in res2["clusters"]:
        rep = cluster["representative_row_id"]
        member_ids = {m["row_id"] for m in cluster["members"]}
        if rep == 0:
            assert 2 not in member_ids  # A-C never paired
        if rep == 2:
            assert 0 not in member_ids


def test_representative_scores_shown():
    df = _df(
        c1=["a", "a", "a"],
        c2=["x", "x", "y"],
        c3=[1, 1, 1],
        c4=[9, 9, 9],
        c5=[0, 0, 0],
    )
    res = dup.analyze_duplicates(df, ["c1", "c2", "c3", "c4", "c5"], [], 80)
    # A-B: c1..c5 all equal -> 100; A-C: c2 differs -> 4/5=80; B-C: c2 differs -> 80
    rep_a = next(c for c in res["clusters"] if c["representative_row_id"] == 0)
    scores = {m["row_id"]: m["score_pct"] for m in rep_a["members"]}
    assert scores[1] == 100 and scores[2] == 80
    # every member carries its score against the representative
    for cluster in res["clusters"]:
        rep = cluster["representative_row_id"]
        for m in cluster["members"]:
            assert "matching_columns" in m and "differing_columns" in m


# -- blocking + estimation ---------------------------------------------------------


def test_blocking_restricts_candidates():
    df = _df(
        region=["us", "us", "eu", "eu"],
        a=["x", "x", "x", "x"],
        b=[1, 1, 1, 1],
    )
    res = dup.analyze_duplicates(df, ["a", "b"], ["region"], 100)
    # identical a,b but in different regions -> 2 groups of 2 (within-block only)
    assert res["summary"]["duplicate_groups"] == 2
    for g in res["groups"]:
        assert len(g["members"]) == 2


def test_estimate_and_attainable_pcts():
    df = _df(a=["x"] * 10 + ["y"] * 5, b=list(range(15)))
    est = dup.estimate_candidates(df, ["a", "b"], [], 80, MARKERS)
    # candidates: col a groups -> C(10,2)+C(5,2)=45+10=55; col b all unique -> 0
    assert est["candidate_pairs"] == 55
    assert est["attainable_pcts"] == [0, 50, 100]  # k=2
    assert est["blocked"] is False


def test_blocked_when_excessive(monkeypatch):
    monkeypatch.setattr(dup, "PAIR_HARD_LIMIT", 10)
    df = _df(a=["x"] * 20, b=[1] * 20)
    res = dup.analyze_duplicates(df, ["a", "b"], [], 80)
    assert res["mode"] == "blocked"


def test_missing_vs_populated_no_match():
    df = _df(a=[None, "x"], b=[1, 1])
    res = dup.analyze_duplicates(df, ["a", "b"], [], 80)
    # a: missing vs "x" -> no match; b: match -> 1/2 = 50 < 80
    assert res["clusters"] == []
    assert res["summary"]["qualifying_pairs"] == 0


# -- resolution ops -----------------------------------------------------------------


def test_merge_records_op():
    df = _df(name=["Varun", "Varun J"], city=["Paris", None], age=[30, 30])
    out = execute_op(
        df,
        OperationSpec(
            op_type="merge_records", stage=Stage.DUPLICATES,
            params={
                "primary_row_id": 0,
                "drop_row_ids": [1],
                "field_values": {"name": "Varun"},  # user-chosen on conflict
            },
        ),
    )
    assert out.height == 1
    row = out.filter(pl.col(ROW_ID_COLUMN) == 0).to_dicts()[0]
    assert row["name"] == "Varun"
    assert row["city"] == "Paris"
