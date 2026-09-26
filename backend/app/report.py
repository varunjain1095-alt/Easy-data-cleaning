"""Human-readable HTML cleaning summary report (architecture section 9.3).

Summarizes affected counts and applied values only - no row-level data is
included by default.
"""

import html
import time
from pathlib import Path

from .duplicates import load_result as load_dup_result
from .opspec import Stage
from .outliers import load_state as load_outlier_state
from .project import Project
from .validation import load_validation

VERSION = "0.1.0"

_STAGE_LABELS = {
    Stage.MISSINGNESS: "Missing values",
    Stage.TYPES: "Data types",
    Stage.NORMALIZATION: "Normalization",
    Stage.COLUMN_NAMES: "Column names & formats",
    Stage.INVALID_VALUES: "Invalid values",
    Stage.STRUCTURES: "Empty/constant structures",
    Stage.DUPLICATES: "Duplicates",
    Stage.OUTCOME: "Outcome declaration",
    Stage.UNIVARIATE: "Univariate screening",
    Stage.BIVARIATE: "Bivariate review",
    Stage.VALIDATION: "Final validation",
}


def _esc(x) -> str:
    return html.escape(str(x))


def _table(headers: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _item_section(project: Project, item: dict) -> str:
    item_dir = project.item_dir(item["item_id"])
    history = project.history(item["item_id"])
    validation = load_validation(item_dir)
    outlier_state = load_outlier_state(item_dir)
    meta = project.item_meta(item["item_id"])
    parts = [f"<h2>{_esc(item['name'])}</h2>"]

    if validation:
        parts.append("<h3>Before / after</h3>")
        parts.append(_table(
            ["Metric", "Before", "After"],
            [
                ["Rows", validation["row_count"]["before"], validation["row_count"]["after"]],
                ["Columns", validation["column_count"]["before"], validation["column_count"]["after"]],
                ["Missing values", validation["missing_values"]["before"], validation["missing_values"]["after"]],
                ["Duplicate rows", validation["duplicate_rows"]["before"], validation["duplicate_rows"]["after"]],
                ["Cells changed", "—", validation["cells_changed"]],
                ["Rows removed", "—", validation["rows_removed"]],
                ["Unresolved flagged outliers", "—", validation.get("flagged_outliers_unresolved") or 0],
            ],
        ))

    ops = history.ops[: history.pointer]
    if ops:
        parts.append("<h3>Transformation history (execution order)</h3>")
        parts.append(_table(
            ["#", "Stage", "Operation", "Parameters"],
            [[o.seq, _STAGE_LABELS.get(o.stage, o.stage), o.op_type, _esc(o.params)] for o in ops],
        ))
    else:
        parts.append("<p class='muted'>No transformations applied.</p>")

    skipped = [s for s, st in meta["stage_states"].items() if st == "skipped"]
    warnings = [s for s, st in meta["stage_states"].items() if st in ("needs_recalculation", "unresolved_warnings")]
    if skipped:
        parts.append(f"<p><strong>Skipped steps:</strong> {_esc(', '.join(skipped))}</p>")
    if warnings:
        parts.append(f"<p class='warn'><strong>Unresolved warnings:</strong> {_esc(', '.join(warnings))}</p>")

    dup = load_dup_result(item_dir)
    if dup and dup.get("summary"):
        parts.append("<h3>Duplicate analysis</h3>")
        parts.append(f"<p>Threshold {dup['threshold']}% on {_esc(dup['columns'])} — {_esc(dup['summary'])}</p>")

    for col, cstate in outlier_state.get("columns", {}).items():
        if cstate.get("flagged_ids"):
            parts.append(
                f"<p>Outliers in <strong>{_esc(col)}</strong>: {len(cstate['flagged_ids'])} flagged, "
                f"{len(cstate.get('classifications', {}))} classified, "
                f"{len(cstate.get('treated_ids', []))} treated (method: {_esc(cstate.get('method', ''))}).</p>"
            )
    outcome = meta.get("outcome")
    if outcome:
        parts.append(f"<p>Outcome variable: <strong>{_esc(outcome['column'])}</strong> {_esc(outcome.get('meaning') or '')}</p>")
    return "\n".join(parts)


def build_html_report(project: Project, acknowledged_warnings: bool = False) -> str:
    items = project.data["items"]
    body = "\n".join(_item_section(project, i) for i in items if i["status"] != "rejected")
    rejected = [i["name"] for i in items if i["status"] == "rejected"]
    rejected_html = (
        f"<p class='warn'>Rejected items (not processed): {_esc(', '.join(rejected))}</p>" if rejected else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Quick Data Cleaner — report</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
table {{ border-collapse: collapse; margin: 0.5rem 0 1rem; font-size: 0.85rem; }}
th, td {{ border: 1px solid #ddd; padding: 4px 10px; text-align: left; }}
th {{ background: #f4f4f4; }}
.warn {{ color: #8a5a00; }}
.muted {{ color: #777; }}
</style></head><body>
<h1>Cleaning summary — {_esc(project.data['source_filename'])}</h1>
<p class="muted">Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · Quick Data Cleaner v{VERSION}
 · project {project.data['project_id']}</p>
{f"<p>Acknowledged unresolved warnings at export: <strong>yes</strong></p>" if acknowledged_warnings else ""}
{rejected_html}
{body}
</body></html>"""
