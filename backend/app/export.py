"""Export plumbing (Phase 3A).

- Cleaned sheets are recreated as value-based tables.
- For .xlsx sources, untouched sheets are preserved by editing the source
  workbook in place-of-copy: only processed/excluded sheets are touched.
- Legacy .xls sources export as .xlsx (values only) - the legacy format is
  never reproduced.
- Exports run as background jobs; files land in the session's exports dir and
  are served only to the owning session.
"""

import json
import zipfile
from pathlib import Path

import polars as pl

from .config import ROW_ID_COLUMN
from .jobs import Job
from .outliers import load_state as load_outlier_state
from .project import Project
from .sanitize import sanitize_frame
from .security import sanitize_filename
from .sessions import Session, _atomic_write_json

EXPORTS_DIRNAME = "exports"


def _display_df(df: pl.DataFrame) -> pl.DataFrame:
    return df.drop(ROW_ID_COLUMN) if ROW_ID_COLUMN in df.columns else df


def export_item_csv(df: pl.DataFrame, path: Path) -> None:
    sanitize_frame(_display_df(df)).write_csv(path)


def _write_sheet(ws, df: pl.DataFrame) -> None:
    df = sanitize_frame(_display_df(df))
    ws.append(df.columns)
    for row in df.iter_rows():
        ws.append(list(row))


def export_workbook(project: Project, path: Path, exclude_items: set[str]) -> None:
    """Reconstruct the workbook. Cleaned sheets become value-based tables;
    untouched sheets are preserved as closely as reasonably possible (for
    .xlsx, byte-for-byte intact via editing the source workbook)."""
    import openpyxl

    kind = project.data["kind"]
    source = project.dir / f"source.{kind}"

    if kind == "xlsx":
        wb = openpyxl.load_workbook(source)
        for item in project.data["items"]:
            name = item["name"]
            if name not in wb.sheetnames:
                continue
            if item["item_id"] in exclude_items or item["status"] == "rejected":
                idx = wb.sheetnames.index(name)
                del wb[name]
                continue
            history = project.history(item["item_id"])
            if not history.ops or history.pointer == 0:
                continue  # untouched: preserve the original sheet entirely
            df = history.working()
            idx = wb.sheetnames.index(name)
            del wb[name]
            ws = wb.create_sheet(name, idx)
            _write_sheet(ws, df)
        wb.save(path)
        return

    # .xls and .csv sources: build a fresh workbook of value tables.
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for item in project.data["items"]:
        if item["item_id"] in exclude_items or item["status"] == "rejected":
            continue
        history = project.history(item["item_id"])
        df = history.working() if history.original_path.exists() else pl.DataFrame()
        ws = wb.create_sheet(item["name"][:31])
        _write_sheet(ws, df)
    wb.save(path)


def _export_filename(project: Project, ext: str) -> str:
    stem = sanitize_filename(Path(project.data["source_filename"]).stem, "dataset")
    return f"{stem}_cleaned.{ext}"


def collect_warnings(project: Project) -> list[str]:
    """Unresolved warnings that gate export until acknowledged (section 9.2)."""
    warnings: list[str] = []
    for item in project.data["items"]:
        if item["status"] == "rejected":
            warnings.append(f"{item['name']}: rejected — {item.get('rejected_reason')}")
            continue
        meta = project.item_meta(item["item_id"])
        for stage, st in meta["stage_states"].items():
            if st in ("needs_recalculation", "unresolved_warnings"):
                warnings.append(f"{item['name']}: stage '{stage}' is {st}")
        unresolved = sum(
            len(set(e.get("flagged_ids", [])) - set(e.get("classifications", {}).keys()) - set(e.get("treated_ids", [])))
            for e in load_outlier_state(project.item_dir(item["item_id"]))["columns"].values()
        )
        if unresolved:
            warnings.append(f"{item['name']}: {unresolved} flagged outlier(s) unresolved")
    return warnings


def _flagged_records(project: Project, exports: Path, exclude: set[str]) -> list[dict]:
    """Separate export of flagged/unresolved records (section 9.4)."""
    from .duplicates import load_result as load_dup_result

    produced = []
    for item in project.data["items"]:
        if item["item_id"] in exclude or item["status"] == "rejected":
            continue
        history = project.history(item["item_id"])
        if not history.original_path.exists():
            continue
        df = history.working()
        reasons: dict[int, list[str]] = {}
        state = load_outlier_state(project.item_dir(item["item_id"]))
        for col, entry in state["columns"].items():
            for rid in entry.get("flagged_ids", []):
                reasons.setdefault(rid, []).append(f"outlier:{col}")
        dup = load_dup_result(project.item_dir(item["item_id"]))
        if dup:
            for g in dup.get("groups", []):
                for rid in g["members"]:
                    reasons.setdefault(rid, []).append("duplicate_group")
            for c in dup.get("clusters", []):
                reasons.setdefault(c["representative_row_id"], []).append("duplicate_cluster_rep")
                for m in c.get("members", []):
                    reasons.setdefault(m["row_id"], []).append("duplicate_cluster_member")
        if not reasons:
            continue
        sub = df.filter(pl.col(ROW_ID_COLUMN).is_in(list(reasons)))
        sub = sub.with_columns(
            pl.col(ROW_ID_COLUMN)
            .map_elements(lambda r: ";".join(reasons.get(r, [])), return_dtype=pl.String)
            .alias("__flag_reason")
        )
        name = f"{sanitize_filename(item['name'], 'sheet')}_flagged.csv"
        path = exports / name
        sanitize_frame(sub.drop(ROW_ID_COLUMN)).write_csv(path)
        produced.append({"name": name, "size": path.stat().st_size, "kind": "flagged"})
    return produced


def run_export(job: Job, session: Session, cfg: dict) -> dict:
    """Job entry: produce export files per cfg.

    cfg: {formats: [csv,xlsx,report,json,script,flagged,zip], exclude_items: [],
          acknowledge_warnings: bool}
    Unresolved warnings require acknowledge_warnings=true; the acknowledgement
    is recorded in the manifest and report.
    """
    from .report import build_html_report
    from .repro import generate_config_json, generate_script
    from .validation import run_validation

    project = Project.load(session)
    if project is None:
        raise ValueError("No project in this session")

    warnings = collect_warnings(project)
    acknowledged = bool(cfg.get("acknowledge_warnings"))
    if warnings and not acknowledged:
        raise ValueError(
            "Unresolved warnings require acknowledgement before export: " + "; ".join(warnings)
        )

    exports = session.path(EXPORTS_DIRNAME)
    exports.mkdir(parents=True, exist_ok=True)
    exclude = set(cfg.get("exclude_items", []))
    formats = cfg.get("formats", ["csv", "xlsx"])
    produced: list[dict] = []

    job.update_progress("Preparing exports")
    if "csv" in formats:
        for i, item in enumerate(project.data["items"]):
            if item["item_id"] in exclude or item["status"] == "rejected":
                continue
            job.update_progress(f"Writing CSV for {item['name']}", done=i, total=len(project.data["items"]))
            history = project.history(item["item_id"])
            if not history.original_path.exists():
                continue
            name = _export_filename(project, "csv") if len(project.data["items"]) == 1 else (
                f"{sanitize_filename(item['name'], 'sheet')}_cleaned.csv"
            )
            path = exports / name
            export_item_csv(history.working(), path)
            produced.append({"name": name, "size": path.stat().st_size, "kind": "csv"})

    if "xlsx" in formats:
        job.update_progress("Reconstructing workbook")
        name = _export_filename(project, "xlsx")
        path = exports / name
        export_workbook(project, path, exclude)
        produced.append({"name": name, "size": path.stat().st_size, "kind": "xlsx"})

    if "report" in formats:
        job.update_progress("Running final validation")
        for item in project.data["items"]:
            if item["item_id"] in exclude or item["status"] == "rejected":
                continue
            history = project.history(item["item_id"])
            if history.original_path.exists():
                run_validation(job, session, item["item_id"])
        name = _export_filename(project, "html").replace("_cleaned", "_report")
        path = exports / name
        path.write_text(build_html_report(project, acknowledged_warnings=acknowledged), encoding="utf-8")
        produced.append({"name": name, "size": path.stat().st_size, "kind": "report"})

    if "json" in formats:
        job.update_progress("Writing transformation config")
        name = _export_filename(project, "json").replace("_cleaned", "_transformations")
        path = exports / name
        _atomic_write_json(path, generate_config_json(project))
        produced.append({"name": name, "size": path.stat().st_size, "kind": "json"})

    if "script" in formats:
        job.update_progress("Generating reproducible script")
        name = _export_filename(project, "py").replace("_cleaned", "_clean")
        path = exports / name
        path.write_text(generate_script(project), encoding="utf-8")
        produced.append({"name": name, "size": path.stat().st_size, "kind": "script"})

    if "flagged" in formats:
        job.update_progress("Exporting flagged records")
        produced.extend(_flagged_records(project, exports, exclude))

    if "zip" in formats:
        job.update_progress("Bundling ZIP")
        name = _export_filename(project, "zip")
        path = exports / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in produced:
                zf.write(exports / f["name"], f["name"])
        produced.append({"name": name, "size": path.stat().st_size, "kind": "zip"})

    manifest = {
        "files": produced,
        "exclude_items": sorted(exclude),
        "warnings": warnings,
        "acknowledged_warnings": acknowledged,
        "created_at": __import__("time").time(),
    }
    _atomic_write_json(exports / "manifest.json", manifest)
    job.update_progress("Done", done=1, total=1)
    return manifest


def list_exports(session: Session) -> list[dict]:
    exports = session.path(EXPORTS_DIRNAME)
    manifest = exports / "manifest.json"
    if not manifest.exists():
        return []
    return json.loads(manifest.read_text(encoding="utf-8"))["files"]


def resolve_export(session: Session, filename: str) -> Path | None:
    """Session-authorized export file resolution (no path escape)."""
    safe = sanitize_filename(filename)
    exports = session.path(EXPORTS_DIRNAME)
    candidate = (exports / safe).resolve()
    if not str(candidate).startswith(str(exports.resolve())):
        return None
    return candidate if candidate.exists() else None
