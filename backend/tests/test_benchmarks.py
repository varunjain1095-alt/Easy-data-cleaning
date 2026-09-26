"""100,000-row performance benchmarks (architecture section 14).

Deselected by default; run with: pytest -m benchmark -s
Targets are generous sanity bounds, not tight budgets - the report printed by
-s is the real deliverable.
"""

import io
import time
import uuid

import polars as pl
import pytest

from app.config import ROW_ID_COLUMN
from app.duplicates import analyze_duplicates, estimate_candidates
from app.analysis.missingness import assess_missingness

pytestmark = pytest.mark.benchmark

N = 100_000


def _dirty_frame(n=N, wide=False) -> pl.DataFrame:
    rng = range(n)
    data = {
        ROW_ID_COLUMN: list(rng),
        "name": [f"  Person{i % 5000}  " for i in rng],
        "email": [f"user{i % 8000}@example.com" for i in rng],
        "age": [str(20 + i % 60) if i % 23 else "N/A" for i in rng],
        "region": [["North", "south", "EAST", "west"][i % 4] for i in rng],
        "signup": [f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}" for i in rng],
        "block": [i % 1000 for i in rng],
    }
    if wide:
        for k in range(20):
            data[f"metric_{k}"] = [float((i * k) % 997) for i in rng]
    return pl.DataFrame(data)


class Timer:
    def __init__(self):
        self.marks = {}
    def mark(self, name, t0):
        self.marks[name] = time.perf_counter() - t0
        print(f"    {name}: {self.marks[name]:.2f}s")


def test_100k_csv_ingest(tmp_path):
    df = _dirty_frame().drop(ROW_ID_COLUMN)
    p = tmp_path / "big.csv"
    df.write_csv(p)
    assert p.stat().st_size < 100 * 1024 * 1024

    from app.ingest import read_csv
    t0 = time.perf_counter()
    head = read_csv(p, "utf-8", ",")
    Timer().mark("csv parse 100k", t0)
    assert head.height == N


def test_100k_missingness_assess(tmp_path):
    df = _dirty_frame()
    t0 = time.perf_counter()
    res = assess_missingness(df)
    Timer().mark("missingness assess 100k", t0)
    age = next(c for c in res["columns"] if c["column"] == "age")
    assert age["missing_count"] > 0


def test_100k_duplicate_detection(tmp_path):
    df = _dirty_frame()
    t0 = time.perf_counter()
    est = estimate_candidates(df, ["name", "email"], ["block"], 90, [])
    Timer().mark("dup estimate 100k", t0)
    assert est["candidate_pairs"] < 5_000_000

    t0 = time.perf_counter()
    res = analyze_duplicates(df, ["name", "email"], ["block"], 100)
    Timer().mark("dup analyze exact 100k (blocked)", t0)
    assert res["summary"]["duplicate_groups"] > 0

    t0 = time.perf_counter()
    res = analyze_duplicates(df, ["name", "email"], ["block"], 90)
    Timer().mark("dup analyze near-90 100k (blocked)", t0)


def test_100k_xlsx_write(tmp_path):
    # workbook reconstruction at scale (openpyxl is the known slow path)
    import openpyxl
    df = _dirty_frame(20_000).drop(ROW_ID_COLUMN)  # 20k keeps the test reasonable
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(df.columns)
    t0 = time.perf_counter()
    for row in df.iter_rows():
        ws.append(list(row))
    p = tmp_path / "big.xlsx"
    wb.save(p)
    Timer().mark("xlsx write 20k rows", t0)
    assert p.exists()
