# Quick Data Cleaner

Product spec: `architecture.md` (source of truth). Implementation follows the
approved phased plan. Implemented: Phase 0 foundation (sessions, job registry,
op-spec history with checkpoint+replay undo, dependency invalidation),
Phase 1 ingestion (streamed quarantined upload, CSV/XLS/XLSX parsing, preview),
Phase 2 cleaning core (missingness, type inference, normalization, basic
cleaning, scalable duplicate detection with representative-anchored clusters),
Phase 3A export/validation scaffolding (job-backed CSV/XLSX/ZIP exports,
.xlsx workbook reconstruction preserving untouched sheets, .xls->.xlsx
conversion, original-vs-cleaned validation runner), Phase 3B contextual
outlier analysis (outcome declaration, univariate screening with IQR/Z/modified-Z/
percentile/manual bounds, representative bivariate review with evidence
classification, review-gated treatments via treat_rows), Phase 4 exports
(HTML report, JSON transformation config, reproducible Polars script with
replay-verified codegen, flagged-records export, formula-injection
protection, unresolved-warning acknowledgement gate), Phase 5 hardening
(rate limiting, env-configurable data dir, Docker/Railway deployment,
security regression suite, 100k-row benchmarks).

## Deployment

- `Dockerfile` (repo root) builds `frontend/dist` then serves it from FastAPI
  via `StaticFiles` mount — single process, same-origin (no CORS in prod).
- `railway.toml` — Docker builder, `/api/health` healthcheck, `PORT` env.
- Mount a Railway Volume at `/data`; `QDC_DATA_DIR` env var (default
  `backend/data`) controls the session storage root. On startup, persisted
  queued/running jobs are marked `interrupted` and are user-restartable.
- Rate limits (in-memory sliding window, `app/ratelimit.py`):
  upload 10/min, item ops 120/min, export 12/min per session/IP.

## Benchmarks (pytest -m benchmark, 100k rows, local dev machine)

- CSV parse: 0.04s · missingness assess: 1.85s · dup estimate: 0.04s
- dup exact (blocked): 0.15s · dup near-90 (blocked): 2.90s
- xlsx write 20k rows: 2.47s (openpyxl is the slow path; exports run as jobs)

## Layout

- `backend/` — FastAPI + Polars
  - `app/sessions.py` — opaque-token sessions, per-session dirs, 1h expiry + cleanup
  - `app/jobs.py` — in-process job registry (thread pool, persisted status,
    interrupted-job recovery; no Celery/Redis)
  - `app/opspec.py` — deterministic operation specs + executor registry
  - `app/history.py` — ordered op log, undo/redo pointer, parquet checkpoints
  - `app/invalidation.py` — section-15 dependency map
  - `app/project.py` — workbook project + per-item state/stage states
  - `app/ingest.py` — streaming upload, signature validation, CSV detection,
    openpyxl (.xlsx) / python-calamine (.xls) readers, per-item row limits
  - `app/masking.py` — recognized missing-value masks (nulls, blanks, markers)
  - `app/analysis/` — read-only assessments: `missingness.py`, `typing.py`,
    `normalize.py`, `basic.py`
  - `app/duplicates.py` — candidate generation (hash/blocking/shared-field),
    representative-anchored clusters, pair-volume estimation
  - `app/export.py` — CSV/XLSX/ZIP export writers, workbook reconstruction
  - `app/validation.py` — original-vs-cleaned comparison checks (job)
  - `app/outliers.py` — two-stage outlier workflow: univariate screen,
    context recommendations, pairwise evidence, per-item state
  - `app/report.py` — HTML summary report (counts only, no row-level data)
  - `app/repro.py` — reproducible Polars script + JSON config generation
  - `app/sanitize.py` — formula-injection escaping for CSV/XLSX exports
  - `app/routes/` — sessions, jobs, project, cleaning, export, outliers
- `frontend/` — React + Vite + TypeScript
- `data/` — runtime session storage (gitignored; created at runtime)

## Commands

```bash
# backend
cd backend
pip install -r requirements.txt
python -m pytest tests -q
python -m uvicorn app.main:app --reload --port 8000

# frontend
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to :8000
npm run build      # typecheck + bundle
```

## Constraints to preserve

- Never execute uploaded macros/embedded code; reject password-protected and
  macro-enabled workbooks; sanitize filenames; formula-injection-safe exports.
- 100 MB enforced during streaming; 100,000 records per selected sheet/table;
  never silently truncate.
- Duplicate detection must not do unrestricted O(n^2): hash for 100%, blocking
  + candidate pairs for 80/90%; clusters are representative-anchored only.
- All cleaning decisions use the full dataset; sampled charts must be labeled.
- Recommendations are advisory; nothing applies without user confirmation.
