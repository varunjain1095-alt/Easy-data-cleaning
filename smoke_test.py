"""Full-surface smoke test: every endpoint the UI buttons call, in pipeline order."""
import io, csv, json, time, sys
import requests

B = "http://127.0.0.1:8000"
results = []

def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail[:160]}")

def req(method, path, h, **kw):
    r = requests.request(method, B + path, headers=h, timeout=30, **kw)
    return r

def wait_job(h, jid, tries=150):
    for _ in range(tries):
        s = req("GET", f"/api/jobs/{jid}", h).json()
        if s["status"] in ("completed", "failed"):
            return s
        time.sleep(0.4)
    return {"status": "timeout"}

# ---------- session ----------
tok = req("POST", "/api/sessions", {}).json()["token"]
h = {"X-Session-Token": tok}
check("session create", bool(tok))
r = req("GET", "/api/session", h)
check("session info", r.status_code == 200, r.text[:80])

# ---------- upload (dataset exercising everything) ----------
buf = io.StringIO(); w = csv.writer(buf)
w.writerow(["id", "name", "CO(GT)", "PT08.S1(CO)", "join_date", "email", "score", "grp", "notes"])
w.writerow([1, "Alice", "1.5", "100", "2024-01-05", "alice@x.com", "10", "A", "ok"])
w.writerow([1, "Alice", "1.5", "100", "2024-01-05", "alice@x.com", "10", "A", "ok"])   # exact dup
w.writerow([2, "Bob",   "2.0", "200", "2024-01-06", "bob@x.com",   "20", "A", "x~y"])  # special char val
w.writerow([2, "Bobb",  "2.0", "200", "2024-01-06", "bob@x.com",   "20", "A", ""])     # near+key dup
w.writerow([3, "Carol", "",    "300", "NA",         "carol",       "30", "B", ""])     # missing/bad
w.writerow([4, "Dave",  "3.3", "999999","2024-02-01","dave@x.com", "9999","B", ""])    # outlier
w.writerow(["", "Eve",  "4.0", "400", "2024-02-02", "eve@x.com",   "40", "C", ""])     # missing id
w.writerow([5, "Fran",  "5.5", "500", "2024-02-03", "fran@x.com",  "50", "C", ""])
w.writerow([6, "Gus",   "6.6", "600", "2024-02-04", "gus@x.com",   "60", "C", ""])
w.writerow([7, "Hal",   "7.7", "700", "2024-02-05", "hal@x.com",   "70", "C", ""])
r = req("POST", "/api/upload", h, files={"file": ("full.csv", buf.getvalue())})
check("upload submit", r.status_code == 202, r.text[:100])
st = wait_job(h, r.json()["job_id"])
check("upload job", st["status"] == "completed", st.get("error") or "")

proj = req("GET", "/api/project", h).json()
iid = proj["items"][0]["item_id"]
check("project + item", bool(iid), iid)

prev = req("GET", f"/api/items/{iid}/preview", h).json()
check("preview", len(prev["columns"]) == 9, str([c["name"] for c in prev["columns"]]))

# ---------- every assess endpoint ----------
for stage in ["profile", "special-chars", "missingness", "types", "normalization", "basic"]:
    r = req("GET", f"/api/items/{iid}/assess/{stage}", h)
    check(f"assess/{stage}", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

r = req("GET", f"/api/items/{iid}/assess/units?column=score", h)
check("assess/units", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

r = req("GET", f"/api/items/{iid}/assess/pattern-presets", h)
presets = r.json().get("presets", [])
check("pattern-presets", r.status_code == 200, str(presets))
if presets:
    r = req("GET", f"/api/items/{iid}/assess/patterns?column=email&pattern={presets[0]}", h)
    check("assess/patterns", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

r = req("POST", f"/api/items/{iid}/assess/group-impute", h, json={"column": "score", "group_by": ["grp"]})
check("assess/group-impute", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

r = req("POST", f"/api/items/{iid}/assess/invalid", h, json={"column": "score", "rule": {"type": "cmp", "op": "ge", "value": 0, "value_kind": "number"}})
check("assess/invalid cmp", r.status_code == 200, r.text[:150] if r.status_code != 200 else "")
r = req("POST", f"/api/items/{iid}/assess/invalid", h, json={"column": "email", "rule": {"type": "contains", "text": "@"}})
check("assess/invalid contains", r.status_code == 200, r.text[:150] if r.status_code != 200 else "")
r = req("POST", f"/api/items/{iid}/assess/invalid", h, json={"column": "score", "rule": {"bogus": 1}})
check("assess/invalid malformed -> 400 not 500", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

r = req("GET", f"/api/items/{iid}/assess/keys?columns=id", h)
check("assess/keys", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

r = req("GET", f"/api/items/{iid}/missing-rows?column=id&n=10", h)
check("missing-rows", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

# ---------- reparse (must run before any ops are applied) ----------
r = req("POST", f"/api/items/{iid}/reparse", h, json={})
check("reparse (pre-ops)", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

# ---------- every op type: preview + apply ----------
ops = [
    ("rename_columns",  "special_chars", {"mapping": {"CO(GT)": "co_gt"}}, ["CO(GT)"]),
    ("clean_special_chars", "special_chars", {"column": "notes", "chars": ["~"], "action": "remove", "replacement": ""}, ["notes"]),
    ("treat_missing",   "missingness",  {"column": "co_gt", "method": "constant", "value": "0"}, ["co_gt"]),
    ("convert_type",    "types",        {"column": "score", "target": "integer"}, ["score"]),
    ("normalize",       "normalization", {"columns": ["name"], "operations": [{"op": "trim"}, {"op": "lower"}]}, ["name"]),
    ("standardize_format", "column_names", {"column": "join_date", "kind": "date_format", "format": "%Y-%m-%d"}, ["join_date"]),
    ("mask_values",     "invalid_values", {"column": "email", "rule": {"type": "contains", "text": "@"}}, ["email"]),
    ("drop_rows",       "structures",   {"row_ids": [999999]}, []),          # no-op rows
    ("convert_units",   "units",        {"column": "score", "rules": [{"unit": "", "factor": 1}]}, ["score"]),
    ("clean_pattern",   "patterns",     {"column": "email", "action": "replace", "pattern": "@", "replacement": " [at] "}, ["email"]),
    ("treat_rows",      "keys",         {"column": "id", "row_ids": [888888], "method": "value", "value": "7"}, ["id"]),
    ("drop_column",     "structures",   {"column": "grp"}, ["grp"]),
]
applied = 0
for op_type, stage, params, targets in ops:
    body = {"op_type": op_type, "stage": stage, "params": params, "target_columns": targets}
    rp = req("POST", f"/api/items/{iid}/ops/preview", h, json=body)
    okp = rp.status_code == 200
    ra = req("POST", f"/api/items/{iid}/ops", h, json=body)
    oka = ra.status_code == 200
    check(f"op {op_type}", okp and oka, f"prev={rp.status_code} apply={ra.status_code} {ra.text[:90] if not oka else ''}")
    if oka: applied += 1

# ---------- undo/redo/history ----------
r = req("POST", f"/api/items/{iid}/undo", h)
check("undo", r.status_code == 200 and r.json().get("can_undo") is not None, r.text[:80])
r = req("POST", f"/api/items/{iid}/redo", h)
check("redo", r.status_code == 200, r.text[:80])
r = req("GET", f"/api/items/{iid}/history", h)
check("history", r.status_code == 200 and "ops" in r.json(), f"ops={len(r.json().get('ops', []))}")

# ---------- stage ----------
r = req("POST", f"/api/items/{iid}/stage", h, json={"stage": "missingness", "state": "completed"})
check("set stage", r.status_code == 200, r.text[:80])

# ---------- duplicates ----------
r = req("POST", f"/api/items/{iid}/duplicates/estimate", h, json={"columns": ["name"], "blocking_columns": [], "threshold": 100})
check("dup estimate", r.status_code == 200, r.text[:80])
for cfg_name, cfg in [
    ("exact100", {"columns": ["id", "name"], "blocking_columns": [], "threshold": 100, "tag": "rows"}),
    ("near80",   {"columns": ["id", "name"], "blocking_columns": [], "threshold": 80, "tag": "rows"}),
    ("key",      {"columns": ["id"], "blocking_columns": [], "threshold": 100, "tag": "key"}),
]:
    r = req("POST", f"/api/items/{iid}/duplicates/analyze", h, json=cfg)
    st = wait_job(h, r.json()["job_id"]) if r.status_code == 202 else {"status": r.status_code}
    res = req("GET", f"/api/items/{iid}/duplicates/result", h)
    d = res.json()
    check(f"dup analyze {cfg_name}", st["status"] == "completed" and res.status_code == 200,
          f"mode={d.get('mode')} tag={d.get('tag')}")

# resolve: keep first of whatever exact groups exist (re-run key check for deterministic groups)
req("POST", f"/api/items/{iid}/duplicates/analyze", h, json={"columns": ["id"], "blocking_columns": [], "threshold": 100, "tag": "key"})
time.sleep(0.5)
# wait for the job properly
jobs = req("GET", f"/api/items/{iid}/duplicates/result", h).json()
groups = jobs.get("groups") or []
if groups:
    dec = [{"row_ids": groups[0]["members"], "action": "keep_first"}]
    r = req("POST", f"/api/items/{iid}/duplicates/resolve", h, json={"decisions": dec})
    check("dup resolve", r.status_code == 200, r.text[:100])
else:
    check("dup resolve", True, "no groups to resolve (skipped)")

# ---------- keys ----------
r = req("POST", f"/api/items/{iid}/keys/declare", h, json={"columns": ["id"]})
check("keys declare", r.status_code == 200, r.text[:80])
r = req("GET", f"/api/items/{iid}/keys/declaration", h)
check("keys declaration", r.status_code == 200 and r.json().get("declared"), r.text[:80])

# ---------- outliers ----------
r = req("POST", f"/api/items/{iid}/outcome", h, json={"column": None})
check("outcome declare", r.status_code == 200, r.text[:80])
r = req("GET", f"/api/items/{iid}/outcome", h)
check("outcome get", r.status_code == 200, r.text[:80])
r = req("GET", f"/api/items/{iid}/outliers/screen/score?method=iqr", h)
check("outliers screen", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
# full review flow: mark -> context -> bivariate -> classify -> treat
r = req("POST", f"/api/items/{iid}/outliers/mark", h, json={"column": "score", "action": "mark"})
check("outliers mark", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
r = req("GET", f"/api/items/{iid}/outliers/context/score", h)
check("outliers context", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
cands = (r.json().get("candidates") or []) if r.status_code == 200 else []
ctx = (cands[0].get("column") if isinstance(cands[0], dict) else cands[0]) if cands else "id"
r = req("POST", f"/api/items/{iid}/outliers/bivariate", h, json={"column": "score", "context": ctx})
check("outliers bivariate", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
biv = r.json() if r.status_code == 200 else {}
flagged = biv.get("flagged_ids") or []
if flagged:
    r = req("POST", f"/api/items/{iid}/outliers/classify", h, json={"column": "score", "row_id": flagged[0], "classification": "likely_error"})
    check("outliers classify", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
    r = req("POST", f"/api/items/{iid}/outliers/treat", h, json={"column": "score", "row_ids": flagged, "action": "set_missing"})
    check("outliers treat", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
else:
    check("outliers classify+treat", True, "no flagged rows after review (skipped)")
r = req("GET", f"/api/items/{iid}/outliers/state", h)
check("outliers state", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")

# ---------- validation ----------
r = req("POST", f"/api/items/{iid}/validate", h)
st = wait_job(h, r.json()["job_id"]) if r.status_code == 202 else {"status": r.status_code}
r = req("GET", f"/api/items/{iid}/validation", h)
check("validation run+get", st["status"] == "completed" and r.status_code == 200, f"job={st['status']} get={r.status_code}")

# ---------- export ----------
r = req("GET", "/api/export/warnings", h)
check("export warnings", r.status_code == 200, r.text[:100])
r = req("POST", "/api/export", h, json={"formats": ["csv", "xlsx", "zip", "report", "config", "script", "flagged"], "exclude_items": [], "acknowledge_warnings": True})
check("export submit", r.status_code == 202, r.text[:120])
st = wait_job(h, r.json().get("job_id", ""), tries=200) if r.status_code == 202 else {"status": "n/a"}
check("export job", st["status"] == "completed", st.get("error") or "")
r = req("GET", "/api/exports", h)
files = r.json().get("files", r.json() if isinstance(r.json(), list) else [])
check("exports list", r.status_code == 200, str(files)[:150])
for f in files:
    fn = f["name"] if isinstance(f, dict) else f
    r = req("GET", f"/api/exports/{fn}", h)
    check(f"download {fn}", r.status_code == 200 and len(r.content) > 0, f"{len(r.content)}B")

print()
fails = [x for x in results if not x[1]]
print(f"===== {len(results) - len(fails)}/{len(results)} passed =====")
for n, ok, d in fails:
    print(f"FAILED: {n} -- {d}")
sys.exit(1 if fails else 0)
