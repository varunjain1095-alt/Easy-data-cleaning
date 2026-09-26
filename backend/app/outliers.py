"""Contextual outlier analysis (architecture section 8).

Mandatory two-stage workflow:
- Stage 1 univariate screening flags statistically unusual values; treatments
  remain unavailable until bivariate contextual review.
- Stage 2 bivariate review pairs flagged values with a contextual (or
  declared outcome) variable and produces per-record evidence statuses.
- Only after review can records be kept, removed, set missing, capped,
  imputed, or corrected - via deterministic treat_rows ops.

Statistics always use the complete dataset; charts may carry a reproducible
visual sample plus every flagged record (labelled as sampled).
"""

import json
import math
import time
from pathlib import Path

import polars as pl

from .config import ROW_ID_COLUMN
from .masking import is_numeric_dtype, markers_param, missing_expr
from .sessions import _atomic_write_json

DEFAULTS = {
    "iqr_multiplier": 1.5,
    "zscore_threshold": 3.0,
    "modified_z_threshold": 3.5,
    "percentile_lo": 1.0,
    "percentile_hi": 99.0,
}
MIN_GROUP_EVIDENCE = 10          # section 14: min records for a group conclusion
MAX_CONTEXT_MISSING_PCT = 40.0   # section 14: max missingness for suggested context
SCATTER_SAMPLE = 3000            # visual sample cap; flagged always included
SCATTER_SEED = 42


def _numeric_values(df: pl.DataFrame, col: str, markers: list[str]) -> pl.Series:
    """Non-missing numeric view of a column."""
    sub = df.filter(~missing_expr(col, markers))
    s = sub[col]
    if df.schema[col] == pl.String:
        s = s.str.strip_chars().cast(pl.Float64, strict=False).drop_nulls()
    return s.cast(pl.Float64, strict=False).drop_nulls()


def column_stats(df: pl.DataFrame, col: str, markers: list[str]) -> dict:
    s = _numeric_values(df, col, markers)
    if s.len() == 0:
        raise ValueError(f"Column '{col}' has no usable numeric values")
    q1 = float(s.quantile(0.25))
    q3 = float(s.quantile(0.75))
    return {
        "count": s.len(),
        "min": float(s.min()),
        "max": float(s.max()),
        "mean": float(s.mean()),
        "median": float(s.median()),
        "std": float(s.std() or 0.0),
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "skewness": float(s.skew() or 0.0),
    }


def boundaries(stats: dict, method: str, params: dict | None = None) -> tuple[float, float]:
    params = params or {}
    if method == "iqr":
        mult = float(params.get("multiplier", DEFAULTS["iqr_multiplier"]))
        return stats["q1"] - mult * stats["iqr"], stats["q3"] + mult * stats["iqr"]
    if method == "zscore":
        t = float(params.get("threshold", DEFAULTS["zscore_threshold"]))
        return stats["mean"] - t * stats["std"], stats["mean"] + t * stats["std"]
    if method == "manual":
        if params.get("min") is None and params.get("max") is None:
            raise ValueError("manual boundaries require min and/or max")
        return (
            float(params.get("min", float("-inf"))),
            float(params.get("max", float("inf"))),
        )
    raise ValueError(f"Unknown method: {method}")


def _bounds(df: pl.DataFrame, col: str, markers: list[str], method: str, params: dict | None, stats: dict) -> tuple[float, float]:
    params = params or {}
    if method == "modified_zscore":
        t = float(params.get("threshold", DEFAULTS["modified_z_threshold"]))
        s = _numeric_values(df, col, markers)
        med = stats["median"]
        mad = float((s - med).abs().median())
        if mad == 0:
            return stats["min"], stats["max"]
        return med - t * mad / 0.6745, med + t * mad / 0.6745
    if method == "percentile":
        s = _numeric_values(df, col, markers)
        lo = float(params.get("lo", DEFAULTS["percentile_lo"]))
        hi = float(params.get("hi", DEFAULTS["percentile_hi"]))
        return float(s.quantile(lo / 100)), float(s.quantile(hi / 100))
    return boundaries(stats, method, params)


def _histogram(s: pl.Series, bin_count: int = 30) -> list[dict]:
    """Full-data aggregated bins (never sampled)."""
    if s.len() == 0:
        return []
    h = s.hist(bin_count=bin_count)
    return [
        {"edge": float(r["breakpoint"]), "count": int(r["count"]), "label": r["category"]}
        for r in h.to_dicts()
    ]


def univariate_screen(df: pl.DataFrame, col: str, method: str = "iqr", params: dict | None = None) -> dict:
    markers = markers_param(None)
    stats = column_stats(df, col, markers)
    lo, hi = _bounds(df, col, markers, method, params, stats)

    mask = missing_expr(col, markers)
    numeric_col = (
        pl.col(col).cast(pl.Float64, strict=False)
        if df.schema[col] == pl.String
        else pl.col(col).cast(pl.Float64)
    )
    flagged = df.filter(~mask).with_columns(numeric_col.alias("__v")).filter(
        (pl.col("__v") < lo) | (pl.col("__v") > hi)
    )
    flagged_rows = [
        {"row_id": r[ROW_ID_COLUMN], "value": r[col]}
        for r in flagged.select([ROW_ID_COLUMN, col]).to_dicts()
    ]
    s = _numeric_values(df, col, markers)
    return {
        "column": col,
        "method": method,
        "params": params or {},
        "stats": stats,
        "bounds": {"lower": lo, "upper": hi},
        "outlier_count": len(flagged_rows),
        "outlier_pct": round(len(flagged_rows) / df.height * 100, 2) if df.height else 0.0,
        "flagged": flagged_rows,
        "histogram": _histogram(s),
        "treatments_enabled": False,  # locked until bivariate review (8.2)
    }


# ---------------------------------------------------------------------------
# Bivariate contextual review
# ---------------------------------------------------------------------------


def _usable_pairs(df: pl.DataFrame, a: str, b: str, markers: list[str]) -> int:
    mask = ~missing_expr(a, markers) & ~missing_expr(b, markers)
    return int(df.filter(mask).height)


def _is_identifier_like(df: pl.DataFrame, col: str) -> bool:
    return df[col].n_unique() >= max(df.height - 1, 0) and df.height > 0


def _is_free_text(df: pl.DataFrame, col: str) -> bool:
    if df.schema[col] != pl.String:
        return False
    s = df[col].drop_nulls()
    if s.len() == 0:
        return False
    avg_len = float(s.str.len_chars().mean() or 0)
    return avg_len > 40 and s.n_unique() / s.len() > 0.5


def recommend_context(df: pl.DataFrame, col: str, markers: list[str], outcome: dict | None = None) -> list[dict]:
    """Candidate context variables for bivariate review (8.3)."""
    out = []
    n = df.height
    for c in df.columns:
        if c in (col, ROW_ID_COLUMN):
            continue
        miss_pct = missing_expr_count(df, c, markers) / n * 100 if n else 0
        if miss_pct > MAX_CONTEXT_MISSING_PCT:
            continue
        if _is_identifier_like(df, c):
            continue
        if df[c].drop_nulls().n_unique() <= 1:
            continue  # constant column
        if _is_free_text(df, c):
            continue
        usable = _usable_pairs(df, col, c, markers)
        if usable < 2:
            continue
        dtype = df.schema[c]
        if is_numeric_dtype(dtype) or _numeric_values(df, c, markers).len() / max(usable, 1) > 0.9:
            kind = "numeric"
            reason = "numeric association possible"
        elif dtype in (pl.Date, pl.Datetime, pl.Time):
            kind = "datetime"
            reason = "temporal ordering may explain extremes"
        elif dtype == pl.Boolean or df[c].drop_nulls().n_unique() == 2:
            kind = "boolean"
            reason = "two-group comparison"
        else:
            # categorical: need groups with sufficient observations
            counts = df.group_by(c).agg(pl.len()).filter(pl.len() >= MIN_GROUP_EVIDENCE)
            if counts.height == 0:
                continue
            kind = "categorical"
            reason = f"{counts.height} groups with ≥{MIN_GROUP_EVIDENCE} records"
        entry = {"column": c, "kind": kind, "reason": reason, "usable_pairs": usable}
        if outcome and c == outcome.get("column"):
            entry["is_outcome"] = True
            entry["reason"] = "declared outcome variable - " + reason
        out.append(entry)
    # declared outcome recommended first
    out.sort(key=lambda e: (not e.get("is_outcome", False), -e["usable_pairs"]))
    return out


def missing_expr_count(df: pl.DataFrame, col: str, markers: list[str]) -> int:
    return int(df.select(missing_expr(col, markers).sum()).item())


def _scatter_sample(df: pl.DataFrame, x: str, y: str, flagged_ids: set[int]) -> tuple[list[dict], bool]:
    """Reproducible visual sample + every flagged record."""
    total = df.height
    if total <= SCATTER_SAMPLE:
        sample = df
        sampled = False
    else:
        flagged_df = df.filter(pl.col(ROW_ID_COLUMN).is_in(list(flagged_ids)))
        rest = df.filter(~pl.col(ROW_ID_COLUMN).is_in(list(flagged_ids)))
        sample = pl.concat([
            rest.sample(min(SCATTER_SAMPLE, rest.height), seed=SCATTER_SEED),
            flagged_df,
        ])
        sampled = True
    rows = sample.select([ROW_ID_COLUMN, x, y]).to_dicts()
    return rows, sampled


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def bivariate_review(df: pl.DataFrame, col: str, context: str, flagged_ids: list[int], markers: list[str]) -> dict:
    """Pairwise contextual evidence for flagged records (8.3/8.4)."""
    ctx_dtype = df.schema[context]
    numeric_ctx = is_numeric_dtype(ctx_dtype) or (
        ctx_dtype == pl.String
        and _numeric_values(df, context, markers).len() / max(_usable_pairs(df, col, context, markers), 1) > 0.9
    )
    flagged_set = set(flagged_ids)

    pair = df.filter(~missing_expr(col, markers) & ~missing_expr(context, markers))
    usable = pair.height
    base = {
        "column": col,
        "context": context,
        "usable_pairs": usable,
        "insufficient_evidence": usable < MIN_GROUP_EVIDENCE,
        "flagged_ids": flagged_ids,
        "evidence": {},
    }

    if numeric_ctx:
        xs = [float(v) for v in pair[col].cast(pl.Float64, strict=False).to_list()]
        ys = [float(v) for v in pair[context].cast(pl.Float64, strict=False).to_list()]
        r = _pearson(xs, ys)
        # Least-squares trend + residual spread fit on UNFLAGGED pairs only -
        # the flagged records must not bias the relationship they are judged against.
        rid_to_idx = {r: i for i, r in enumerate(pair[ROW_ID_COLUMN].to_list())}
        ux = [x for i, x in enumerate(xs) if pair[ROW_ID_COLUMN][i] not in flagged_set]
        uy = [y for i, y in enumerate(ys) if pair[ROW_ID_COLUMN][i] not in flagged_set]
        slope = intercept = 0.0
        if len(ux) >= 3:
            mx, my = sum(ux) / len(ux), sum(uy) / len(ux)
            sxx = sum((x - mx) ** 2 for x in ux)
            sxy = sum((x - mx) * (y - my) for x, y in zip(ux, uy))
            slope = sxy / sxx if sxx else 0.0
            intercept = my - slope * mx
        residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
        unflagged_res = [y - (slope * x + intercept) for x, y in zip(ux, uy)]
        res_std = _std(unflagged_res) if len(unflagged_res) > 1 else 0.0
        res_med = sorted(unflagged_res)[len(unflagged_res) // 2] if unflagged_res else 0.0
        for rid in flagged_ids:
            i = rid_to_idx.get(rid)
            if i is None:
                base["evidence"][str(rid)] = "insufficient_evidence"
                continue
            res = residuals[i]
            if res_std == 0:
                tol = 1e-6 * max(1.0, abs(res_med))
                status = "explained_by_context" if abs(res - res_med) <= tol else "still_unusual"
            else:
                status = "explained_by_context" if abs(res - res_med) <= 3 * res_std else "still_unusual"
            base["evidence"][str(rid)] = status
        rows, sampled = _scatter_sample(df, context, col, flagged_set)
        base.update({
            "kind": "numeric",
            "correlation": r,
            "trend": {"slope": slope, "intercept": intercept},
            "points": rows,
            "sampled": sampled,
        })
        return base

    if ctx_dtype in (pl.Date, pl.Datetime, pl.Time):
        ordered = pair.sort(context)
        xs = ordered[context].to_list()
        ys = [float(v) for v in ordered[col].cast(pl.Float64, strict=False).to_list()]
        window = 20
        roll_med, roll_lo, roll_hi = [], [], []
        for i in range(len(ys)):
            w = ys[max(0, i - window // 2): i + window // 2 + 1]
            ws = sorted(w)
            roll_med.append(ws[len(ws) // 2])
            roll_lo.append(ws[max(0, int(len(ws) * 0.25))])
            roll_hi.append(ws[min(len(ws) - 1, int(len(ws) * 0.75))])
        rid_idx = {r: i for i, r in enumerate(ordered[ROW_ID_COLUMN].to_list())}
        for rid in flagged_ids:
            i = rid_idx.get(rid)
            if i is None or usable < MIN_GROUP_EVIDENCE:
                base["evidence"][str(rid)] = "insufficient_evidence"
                continue
            status = "explained_by_context" if roll_lo[i] <= ys[i] <= roll_hi[i] else "still_unusual"
            base["evidence"][str(rid)] = status
        points = [
            {"x": str(x), "y": y, "row_id": rid, "roll_median": m, "roll_lo": l, "roll_hi": h}
            for x, y, rid, m, l, h in zip(xs, ys, ordered[ROW_ID_COLUMN].to_list(), roll_med, roll_lo, roll_hi)
        ]
        base.update({"kind": "datetime", "points": points[:SCATTER_SAMPLE],
                     "sampled": len(points) > SCATTER_SAMPLE})
        return base

    # categorical / boolean: group stats + flagged position within group
    grp = (
        pair.group_by(context)
        .agg([
            pl.len().alias("n"),
            pl.col(col).cast(pl.Float64, strict=False).median().alias("median"),
            pl.col(col).cast(pl.Float64, strict=False).quantile(0.25).alias("q1"),
            pl.col(col).cast(pl.Float64, strict=False).quantile(0.75).alias("q3"),
        ])
    )
    groups = {str(r[context]): r for r in grp.to_dicts()}
    rid_row = {r[ROW_ID_COLUMN]: r for r in pair.iter_rows(named=True)}
    for rid in flagged_ids:
        row = rid_row.get(rid)
        if row is None:
            base["evidence"][str(rid)] = "insufficient_evidence"
            continue
        g = groups.get(str(row[context]))
        if g is None or g["n"] < MIN_GROUP_EVIDENCE:
            base["evidence"][str(rid)] = "insufficient_evidence"
            continue
        iqr = (g["q3"] or 0) - (g["q1"] or 0)
        v = float(row[col])
        lo, hi = g["q1"] - 1.5 * iqr, g["q3"] + 1.5 * iqr
        base["evidence"][str(rid)] = "explained_by_context" if lo <= v <= hi else "still_unusual"
    base.update({
        "kind": "categorical" if ctx_dtype != pl.Boolean else "boolean",
        "groups": [
            {"value": k, "n": v["n"], "median": v["median"], "q1": v["q1"], "q3": v["q3"],
             "iqr": (v["q3"] or 0) - (v["q1"] or 0)}
            for k, v in groups.items()
        ],
        "group_distributions": {
            k: pair.filter(pl.col(context).cast(pl.String) == k)[col]
            .cast(pl.Float64, strict=False).to_list()[:200]
            for k in list(groups)[:12]
        },
    })
    return base


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# ---------------------------------------------------------------------------
# Per-item outlier state persistence
# ---------------------------------------------------------------------------


def _state_path(item_dir: Path) -> Path:
    return item_dir / "outliers.json"


def load_state(item_dir: Path) -> dict:
    p = _state_path(item_dir)
    if not p.exists():
        return {"columns": {}, "evidence_log": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(item_dir: Path, state: dict) -> None:
    _atomic_write_json(_state_path(item_dir), state)


def set_column_state(item_dir: Path, col: str, patch: dict) -> dict:
    state = load_state(item_dir)
    entry = state["columns"].setdefault(col, {})
    entry.update(patch)
    save_state(item_dir, state)
    return state


def record_evidence(item_dir: Path, col: str, context: str, evidence: dict) -> dict:
    """Retain each pairwise analysis so evidence across contexts can be compared."""
    state = load_state(item_dir)
    state["evidence_log"].setdefault(col, {})[context] = {
        "evidence": evidence,
        "at": time.time(),
    }
    save_state(item_dir, state)
    return state


def classify_record(item_dir: Path, col: str, row_id: int, classification: str) -> dict:
    state = load_state(item_dir)
    entry = state["columns"].setdefault(col, {})
    entry.setdefault("classifications", {})[str(row_id)] = {
        "classification": classification,
        "at": time.time(),
    }
    save_state(item_dir, state)
    return state


def unresolved_outliers(item_dir: Path) -> int:
    """Flagged records neither classified nor treated - feeds final validation."""
    state = load_state(item_dir)
    count = 0
    for col, entry in state["columns"].items():
        flagged = set(entry.get("flagged_ids", []))
        decided = set(entry.get("classifications", {})) | set(entry.get("treated_ids", []))
        count += len(flagged - decided)
    return count
