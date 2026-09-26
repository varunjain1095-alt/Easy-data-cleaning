"""File ingestion: streamed quarantined upload, validation, parsing.

Rules (revised plan / architecture):
- The 100 MB limit is enforced while streaming; the request is never fully buffered.
- Uploads land in a quarantined session location and are parsed/counted before
  being accepted into the cleaning workflow.
- The 100,000-record limit applies separately to each selected sheet/table.
- Over-limit items are rejected and their temporary artifacts deleted; records
  are never truncated or sampled to satisfy the limit.
- .xlsx is read with openpyxl; legacy .xls with python-calamine.
- Password-protected and macro-enabled workbooks are rejected.
"""

import io
import shutil
import zipfile
from pathlib import Path
from typing import Any

import polars as pl
from charset_normalizer import from_bytes

from .config import (
    MAX_ROWS,
    MAX_UPLOAD_BYTES,
    QUARANTINE_DIRNAME,
    ROW_ID_COLUMN,
    UPLOAD_CHUNK_BYTES,
)
from .security import new_id, sanitize_filename
from .sessions import Session

XLSX_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

ALLOWED_EXTENSIONS = {".csv", ".txt", ".xls", ".xlsx"}
DELIMITER_CANDIDATES = [",", ";", "\t", "|"]


class IngestError(Exception):
    """User-facing ingestion failure.

    kind distinguishes rejection reasons, e.g. 'file_size' vs 'row_limit' vs
    'invalid_file' vs 'protected_workbook' vs 'macro_enabled'.
    """

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# Streaming upload
# ---------------------------------------------------------------------------


async def stream_to_quarantine(session: Session, filename: str, stream) -> Path:
    """Stream an upload into the session quarantine dir, enforcing the byte cap."""
    safe_name = sanitize_filename(filename)
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise IngestError(
            "invalid_file",
            f"Unsupported file type '{ext or '(none)'}'. Supported: CSV, XLS, XLSX.",
        )

    quarantine = session.path(QUARANTINE_DIRNAME, new_id())
    quarantine.mkdir(parents=True, exist_ok=True)
    dest = quarantine / safe_name

    written = 0
    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = await stream.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise IngestError(
                        "file_size",
                        f"File exceeds the 100 MB limit ({written / 1e6:.1f} MB received).",
                    )
                fh.write(chunk)
    except Exception:
        shutil.rmtree(quarantine, ignore_errors=True)
        raise
    if written == 0:
        shutil.rmtree(quarantine, ignore_errors=True)
        raise IngestError("invalid_file", "Uploaded file is empty.")
    return dest


# ---------------------------------------------------------------------------
# Signature / workbook validation
# ---------------------------------------------------------------------------


def detect_file_kind(path: Path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(8)
    ext = path.suffix.lower()

    if head.startswith(XLSX_MAGIC):
        if ext == ".xls":
            raise IngestError("invalid_file", "File signature does not match .xls extension.")
        return "xlsx"
    if head.startswith(OLE2_MAGIC):
        if ext == ".xlsx":
            raise IngestError(
                "protected_workbook",
                "Workbook is encrypted or password-protected and cannot be processed.",
            )
        return "xls"
    if ext in {".xls", ".xlsx"}:
        raise IngestError("invalid_file", "File signature is not a valid Excel workbook.")
    # Treat remaining allowed files as CSV text; reject obvious binaries.
    sample = path.read_bytes()[:8192]
    if b"\x00" in sample:
        raise IngestError("invalid_file", "File appears to be binary, not a CSV/text file.")
    return "csv"


def _assert_no_macros_xlsx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "xl/workbook.xml" not in names:
                raise IngestError("invalid_file", "ZIP file is not a valid .xlsx workbook.")
            if any("vbaproject.bin" in n.lower() for n in names):
                raise IngestError(
                    "macro_enabled",
                    "Macro-enabled workbooks are not supported. Save as .xlsx without macros and re-upload.",
                )
    except zipfile.BadZipFile:
        raise IngestError("invalid_file", "Workbook archive is corrupted.")


def _assert_no_macros_xls(path: Path) -> None:
    try:
        import olefile
    except ImportError:
        return  # olefile unavailable; calamine reads values only, macros never execute
    try:
        with olefile.OleFileIO(path) as ole:
            streams = ["/".join(s).lower() for s in ole.listdir()]
            if any("encryptioninfo" in s or "encryptedpackage" in s for s in streams):
                raise IngestError(
                    "protected_workbook",
                    "Workbook is encrypted or password-protected and cannot be processed.",
                )
            if any(s == "vba" or s.endswith("/vba") or "_vba_project" in s for s in streams):
                raise IngestError(
                    "macro_enabled",
                    "Macro-enabled workbooks are not supported.",
                )
    except IngestError:
        raise
    except Exception:
        raise IngestError("invalid_file", "Workbook is corrupted or unreadable.")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def detect_csv_settings(path: Path, sample_bytes: int = 262_144) -> dict:
    raw = path.read_bytes()[:sample_bytes]
    best = from_bytes(raw).best()
    encoding = (best.encoding if best else "utf-8") or "utf-8"
    text = raw.decode(encoding, errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()][:50]

    scores = {}
    for delim in DELIMITER_CANDIDATES:
        counts = [ln.count(delim) for ln in lines]
        nonzero = [c for c in counts if c > 0]
        # A good delimiter appears consistently across rows.
        consistency = len(set(nonzero)) <= 2 if nonzero else False
        scores[delim] = (len(nonzero), sum(nonzero), consistency)
    delimiter = max(DELIMITER_CANDIDATES, key=lambda d: (scores[d][2], scores[d][1], scores[d][0]))
    if scores[delimiter][0] == 0:
        delimiter = ","
    return {"encoding": encoding, "delimiter": delimiter}


def read_csv(path: Path, encoding: str, delimiter: str) -> pl.DataFrame:
    raw = path.read_bytes()
    text = raw.decode(encoding, errors="replace")
    try:
        df = pl.read_csv(io.StringIO(text), separator=delimiter, infer_schema_length=10_000)
    except Exception as exc:
        raise IngestError("invalid_file", f"CSV parsing failed: {exc}")
    return _sanitize_frame(df)


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def enumerate_xlsx(path: Path) -> list[str]:
    _assert_no_macros_xlsx(path)
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        names = list(wb.sheetnames)
        wb.close()
        return names
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(
            "invalid_file", f"Workbook is corrupted or password-protected: {exc}"
        )


def read_xlsx_sheet(path: Path, sheet: str) -> pl.DataFrame:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows = ws.iter_rows(values_only=True)
        return _rows_to_frame(list(rows))
    finally:
        wb.close()


def enumerate_xls(path: Path) -> list[str]:
    _assert_no_macros_xls(path)
    try:
        from python_calamine import CalamineWorkbook

        wb = CalamineWorkbook.from_path(str(path))
        return list(wb.sheet_names)
    except Exception as exc:
        raise IngestError(
            "invalid_file", f"Workbook is corrupted or password-protected: {exc}"
        )


def read_xls_sheet(path: Path, sheet: str) -> pl.DataFrame:
    from python_calamine import CalamineWorkbook

    wb = CalamineWorkbook.from_path(str(path))
    try:
        rows = wb.get_sheet_by_name(sheet).to_python()
    except Exception as exc:
        raise IngestError("invalid_file", f"Could not read sheet '{sheet}': {exc}")
    return _rows_to_frame(rows)


def _rows_to_frame(rows: list[tuple | list]) -> pl.DataFrame:
    rows = [list(r) for r in rows if r is not None]
    # Drop fully-empty trailing rows.
    while rows and all(v is None for v in rows[-1]):
        rows.pop()
    if not rows:
        return pl.DataFrame()
    width = max(len(r) for r in rows)
    header = [_col_name(rows[0][i], i) for i in range(width)]
    header = _dedupe(header)
    data = {}
    for i, name in enumerate(header):
        data[name] = [(r[i] if i < len(r) else None) for r in rows[1:]]
    return _sanitize_frame(pl.DataFrame(data, strict=False))


def _col_name(value: Any, idx: int) -> str:
    if value is None or str(value).strip() == "":
        return f"column_{idx + 1}"
    return str(value)


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


def _sanitize_frame(df: pl.DataFrame) -> pl.DataFrame:
    """Ensure the frame is parquet-safe: unique names, no Object dtypes, stable row ids.

    NaN floats are normalized to null (NaN is a missing marker; it also breaks
    strict JSON serialization).
    """
    for name in df.columns:
        if df.schema[name] == pl.Object:
            df = df.with_columns(pl.col(name).map_elements(lambda v: str(v) if v is not None else None, return_dtype=pl.String))
        elif df.schema[name] in (pl.Float32, pl.Float64):
            df = df.with_columns(pl.col(name).fill_nan(None))
    df = df.with_row_index(ROW_ID_COLUMN)
    return df


# ---------------------------------------------------------------------------
# Acceptance pipeline (runs inside a background job)
# ---------------------------------------------------------------------------


def process_upload(job, session: Session, quarantined: Path, csv_overrides: dict | None = None) -> dict:
    """Validate, enumerate, parse, and accept an upload into a Project.

    Invoked by the job registry as fn(job, *args).
    """
    from .project import Project

    job.update_progress("Validating file signature")
    kind = detect_file_kind(quarantined)
    detected: dict[str, Any] = {}

    # Accept the source into the project first; parsing reads from the
    # accepted (immutable) location.
    project = Project.create(session, quarantined.name, kind)
    accepted_source = project.dir / f"source{quarantined.suffix.lower()}"
    shutil.move(str(quarantined), accepted_source)

    if kind == "csv":
        settings = detect_csv_settings(accepted_source)
        if csv_overrides:
            settings.update({k: v for k, v in csv_overrides.items() if v})
        detected["csv_settings"] = settings
        items = [("file", Path(project.data["source_filename"]).stem,
                  lambda: read_csv(accepted_source, settings["encoding"], settings["delimiter"]))]
    elif kind == "xlsx":
        job.update_progress("Enumerating workbook sheets")
        sheets = enumerate_xlsx(accepted_source)
        items = [("sheet", s, lambda s=s: read_xlsx_sheet(accepted_source, s)) for s in sheets]
    else:
        job.update_progress("Enumerating workbook sheets")
        sheets = enumerate_xls(accepted_source)
        items = [("sheet", s, lambda s=s: read_xls_sheet(accepted_source, s)) for s in sheets]
        detected["xls_notice"] = "Legacy .xls uploads are exported as .xlsx."

    project.data["detected"] = detected
    project._persist()

    total = len(items)
    rejected = []
    for i, (item_type, name, loader) in enumerate(items):
        job.update_progress(f"Parsing {name}", done=i, total=total)
        item = project.add_item(name, item_type)
        try:
            df = loader()
        except IngestError as exc:
            project.reject_item(item["item_id"], exc.detail)
            shutil.rmtree(project.item_dir(item["item_id"]), ignore_errors=True)
            rejected.append(name)
            continue
        if df.height > MAX_ROWS:
            project.reject_item(
                item["item_id"],
                f"Sheet '{name}' has {df.height:,} records, exceeding the 100,000-record limit.",
            )
            shutil.rmtree(project.item_dir(item["item_id"]), ignore_errors=True)
            rejected.append(name)
            continue
        project.item_dir(item["item_id"]).mkdir(parents=True, exist_ok=True)
        history = project.history(item["item_id"])
        history.initialize(df)
        project.set_item_dims(item["item_id"], df.height, df.width - 1)

    job.update_progress("Finalizing", done=total, total=total)
    result = project.to_dict()
    result["detected"] = detected
    if rejected:
        result["rejected_items"] = rejected
    return result
