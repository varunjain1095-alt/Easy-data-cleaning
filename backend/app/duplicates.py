"""Scalable duplicate detection (architecture section 6, revised plan).

- 100% matches: hash the selected comparison columns -> equivalence groups.
- 80%/90%: candidate generation (optional blocking columns, else shared
  field-value candidates) before similarity scoring. Never unrestricted O(n^2).
- Similarity = matching selected columns / total selected columns * 100.
- Two recognized missing values count as a matching field; missing vs
  populated does not.
- Near-duplicate clusters are representative-anchored ONLY: every member must
  meet the threshold against the designated representative. No unrestricted
  connected components (80/90% similarity is not transitive).
- Field-level equality only; no fuzzy text similarity in Version 1.
"""

import itertools
import json
import math
from pathlib import Path

import polars as pl

from .config import ROW_ID_COLUMN
from .masking import markers_param, normalize_missing
from .sessions import _atomic_write_json

PAIR_WARN_LIMIT = 1_000_000
PAIR_HARD_LIMIT = 25_000_000
MAX_STORED_CLUSTERS = 20_000
MAX_CLUSTER_MEMBERS = 200


def attainable_pcts(k: int) -> list[int]:
    """Match percentages actually reachable with k comparison columns."""
    return sorted({round(100 * m / k) for m in range(k + 1)})


def _c2(n: int) -> int:
    return n * (n - 1) // 2


def _value_groups(df: pl.DataFrame, cols: list[str]) -> list[list[int]]:
    """Row-id lists grouped by equal values across the given columns."""
    grouped = (
        df.group_by(cols)
        .agg(pl.col(ROW_ID_COLUMN).alias("__ids"))
        .select("__ids")
        .to_series()
        .to_list()
    )
    return [g for g in grouped if len(g) > 1]


def _prep(df: pl.DataFrame, columns: list[str], markers: list[str]) -> pl.DataFrame:
    """Normalize missing markers so missing==missing in comparisons."""
    for c in set(columns):
        df = normalize_missing(df, c, markers)
    return df


def estimate_candidates(df: pl.DataFrame, columns: list[str], blocking: list[str], threshold: int, markers: list[str]) -> dict:
    """Estimate candidate-pair volume BEFORE execution (revised plan)."""
    df = _prep(df, columns + blocking, markers)
    if threshold == 100:
        # blocking constrains exact matching too: equality groups are computed
        # within blocks (blocking cols + comparison cols form the group key)
        groups = _value_groups(df, blocking + columns)
        pairs = sum(_c2(len(g)) for g in groups)
        basis = "exact_hash"
    elif blocking:
        groups = _value_groups(df, blocking)
        pairs = sum(_c2(len(g)) for g in groups)
        basis = "blocking_columns"
    else:
        pairs = 0
        for c in columns:
            groups = _value_groups(df, [c])
            pairs += sum(_c2(len(g)) for g in groups)
        basis = "shared_field_candidates"
    return {
        "candidate_pairs": pairs,
        "basis": basis,
        "attainable_pcts": attainable_pcts(len(columns)),
        "warning": pairs > PAIR_WARN_LIMIT,
        "blocked": pairs > PAIR_HARD_LIMIT,
        "hard_limit": PAIR_HARD_LIMIT,
    }


def _accumulate(df: pl.DataFrame, columns: list[str], counts: dict[tuple[int, int], int], job=None, done_base=0) -> None:
    """For each comparison column, add +1 to every pair sharing a value."""
    for ci, c in enumerate(columns):
        for ids in _value_groups(df, [c]):
            ids_sorted = sorted(ids)
            for a, b in itertools.combinations(ids_sorted, 2):
                key = (a, b)
                counts[key] = counts.get(key, 0) + 1
        if job is not None:
            job.update_progress("Scoring candidates", done=done_base + ci + 1, total=done_base + len(columns))


def analyze_duplicates(df: pl.DataFrame, columns: list[str], blocking: list[str], threshold: int, params: dict | None = None, job=None) -> dict:
    """Run duplicate detection. Returns a reviewable result (no mutation)."""
    markers = markers_param(params)
    k = len(columns)
    need = math.ceil(threshold * k / 100)  # fields that must match

    df = _prep(df, columns + blocking, markers)
    id_pos = {rid: i for i, rid in enumerate(df[ROW_ID_COLUMN].to_list())}

    if job:
        job.update_progress("Estimating candidate pairs")
    est = estimate_candidates(df, columns, blocking, threshold, markers)
    if est["blocked"]:
        return {
            "mode": "blocked",
            "estimate": est,
            "reason": "Configuration would generate excessive candidate pairs; add blocking columns or reduce comparison columns.",
        }

    result = {
        "mode": "exact" if threshold == 100 else "near",
        "threshold": threshold,
        "columns": columns,
        "blocking_columns": blocking,
        "attainable_pcts": est["attainable_pcts"],
        "estimate": est,
        "groups": [],
        "clusters": [],
        "summary": {},
    }

    if threshold == 100:
        if job:
            job.update_progress("Hashing exact-duplicate groups")
        groups = _value_groups(df, blocking + columns)
        result["groups"] = [
            {"members": sorted(g), "match_pct": 100, "differing_columns": []}
            for g in groups[:MAX_STORED_CLUSTERS]
        ]
        affected = sum(len(g) for g in groups)
        result["summary"] = {
            "duplicate_groups": len(groups),
            "affected_rows": affected,
            "proposed_removals": affected - len(groups),
            "resulting_rows": df.height - (affected - len(groups)),
        }
        return result

    # --- near duplicates: accumulate field-match counts over candidates ---
    counts: dict[tuple[int, int], int] = {}
    if job:
        job.update_progress("Generating candidate pairs")
    if blocking:
        blocks = df.partition_by(blocking, as_dict=True)
        for bi, (_, block) in enumerate(blocks.items()):
            _accumulate(block, columns, counts, job, bi)
    else:
        _accumulate(df, columns, counts, job)

    qualifying = {p: c for p, c in counts.items() if c >= need}
    if job:
        job.update_progress("Building clusters")

    # Representative-anchored clusters only. A cluster is included when every
    # member meets the threshold against the designated representative.
    adjacency: dict[int, dict[int, int]] = {}
    for (a, b), c in qualifying.items():
        adjacency.setdefault(a, {})[b] = c
        adjacency.setdefault(b, {})[a] = c

    reps = sorted(adjacency, key=lambda r: (-len(adjacency[r]), r))
    involved = set(adjacency)
    sub = df.filter(pl.col(ROW_ID_COLUMN).is_in(list(involved)))
    row_lookup = {r[ROW_ID_COLUMN]: r for r in sub.iter_rows(named=True)}

    clusters = []
    for rep in reps[:MAX_STORED_CLUSTERS]:
        rep_row = row_lookup[rep]
        members = []
        for partner, matches in sorted(adjacency[rep].items()):
            prow = row_lookup[partner]
            differing = [
                c for c in columns
                if not _values_equal(rep_row[c], prow[c])
            ]
            members.append({
                "row_id": partner,
                "score_pct": round(100 * matches / k),
                "matching_columns": [c for c in columns if c not in differing],
                "differing_columns": differing,
            })
        clusters.append({
            "representative_row_id": rep,
            "member_count": len(members),
            "members": members[:MAX_CLUSTER_MEMBERS],
            "note": "Scores are against the representative record; members are not guaranteed to match each other.",
        })

    affected = len(involved)
    result["clusters"] = clusters
    result["summary"] = {
        "qualifying_pairs": len(qualifying),
        "clusters": len(clusters),
        "affected_rows": affected,
        "resulting_rows_if_all_members_removed": df.height - len(qualifying),
    }
    return result


def _values_equal(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a) == str(b)


def save_result(item_dir: Path, result: dict) -> None:
    _atomic_write_json(item_dir / "duplicates_result.json", result)


def load_result(item_dir: Path) -> dict | None:
    p = item_dir / "duplicates_result.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def completeness(df: pl.DataFrame, row_id: int, columns: list[str], markers: list[str]) -> int:
    """Count populated (non-missing) fields - used by keep_most_complete."""
    row = df.filter(pl.col(ROW_ID_COLUMN) == row_id)
    if row.height == 0:
        return 0
    count = 0
    for c in columns:
        v = row[c][0]
        if v is None:
            continue
        if str(v).strip().lower() in markers:
            continue
        count += 1
    return count
