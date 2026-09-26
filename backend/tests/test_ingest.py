import asyncio
import io
import zipfile
from pathlib import Path

import polars as pl
import pytest

import app.ingest as ingest
from app.ingest import (
    IngestError,
    detect_csv_settings,
    detect_file_kind,
    process_upload,
    stream_to_quarantine,
)
from app.jobs import Job
from app.project import Project


class FakeJob:
    def update_progress(self, *a, **k):
        pass


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


def _write_csv(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- CSV detection -----------------------------------------------------------


def test_detect_semicolon_delimiter(tmp_path):
    p = _write_csv(tmp_path / "data.csv", "a;b;c\n1;2;3\n4;5;6\n")
    settings = detect_csv_settings(p)
    assert settings["delimiter"] == ";"


def test_detect_comma_default(tmp_path):
    p = _write_csv(tmp_path / "data.csv", "a,b,c\n1,2,3\n")
    assert detect_csv_settings(p)["delimiter"] == ","


def test_binary_file_rejected(tmp_path):
    p = tmp_path / "data.csv"
    p.write_bytes(b"a,b\n\x00\x00\x00\n")
    with pytest.raises(IngestError) as exc:
        detect_file_kind(p)
    assert exc.value.kind == "invalid_file"


# -- streaming upload ---------------------------------------------------------


def test_stream_enforces_size_limit(session, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_UPLOAD_BYTES", 10)
    stream = FakeStream([b"12345", b"67890", b"more bytes"])
    with pytest.raises(IngestError) as exc:
        asyncio.run(stream_to_quarantine(session, "data.csv", stream))
    assert exc.value.kind == "file_size"


def test_stream_bad_extension_rejected(session):
    stream = FakeStream([b"x"])
    with pytest.raises(IngestError) as exc:
        asyncio.run(stream_to_quarantine(session, "evil.exe", stream))
    assert exc.value.kind == "invalid_file"


def test_filename_sanitized(session):
    stream = FakeStream([b"a,b\n1,2\n"])
    dest = asyncio.run(stream_to_quarantine(session, "../../etc/passwd.csv", stream))
    assert dest.name == "passwd.csv"
    assert session.dir in dest.parents


# -- process_upload: CSV -------------------------------------------------------


def test_process_csv_creates_project(session, tmp_path):
    p = _write_csv(tmp_path / "quar" / "data.csv", "a,b\n1,2\n3,4\n")
    p.parent.mkdir(exist_ok=True)
    result = process_upload(FakeJob(), session, p)
    assert result["kind"] == "csv"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["row_count"] == 2
    assert item["status"] == "not_started"
    project = Project.load(session)
    preview = project.preview(item["item_id"])
    assert preview["row_count"] == 2
    assert preview["columns"][0]["name"] == "a"


def test_row_limit_per_item(session, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ROWS", 2)
    p = _write_csv(tmp_path / "big.csv", "a,b\n" + "\n".join(f"{i},{i}" for i in range(5)))
    result = process_upload(FakeJob(), session, p)
    item = result["items"][0]
    assert item["status"] == "rejected"
    assert "100,000" in item["rejected_reason"] or "exceeding" in item["rejected_reason"]
    # temp artifacts deleted: no parquet files for the rejected item
    assert not list(project_item_dirs(session, item["item_id"]))


def project_item_dirs(session, item_id):
    return (session.dir / "project" / "items" / item_id).glob("*") if (session.dir / "project" / "items" / item_id).exists() else []


# -- process_upload: XLSX -------------------------------------------------------


def _make_xlsx(path: Path, sheets: dict[str, list[list]]):
    import openpyxl

    wb = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet(name)
        if first:
            ws.title = name
            first = False
        for row in rows:
            ws.append(row)
    wb.save(path)


def test_xlsx_multi_sheet_items(session, tmp_path):
    p = tmp_path / "book.xlsx"
    _make_xlsx(p, {"Sheet1": [["a", "b"], [1, 2]], "Data": [["x"], [9], [8]]})
    result = process_upload(FakeJob(), session, p)
    assert result["kind"] == "xlsx"
    names = [i["name"] for i in result["items"]]
    assert names == ["Sheet1", "Data"]
    assert result["items"][1]["row_count"] == 2
    # each item has an independent history
    for item in result["items"]:
        h = project_history(session, item["item_id"])
        assert h.pointer == 0 and h.working().height >= 1


def project_history(session, item_id):
    project = Project.load(session)
    return project.history(item_id)


def test_xlsx_per_sheet_row_limit(session, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ROWS", 1)
    p = tmp_path / "book.xlsx"
    _make_xlsx(p, {"Small": [["a"], [1]], "Big": [["b"], [1], [2], [3]]})
    result = process_upload(FakeJob(), session, p)
    statuses = {i["name"]: i["status"] for i in result["items"]}
    assert statuses["Small"] == "not_started"
    assert statuses["Big"] == "rejected"


def test_macro_enabled_xlsx_rejected(session, tmp_path):
    p = tmp_path / "book.xlsx"
    _make_xlsx(p, {"S": [["a"], [1]]})
    # inject vbaProject.bin to simulate a macro-enabled workbook
    with zipfile.ZipFile(p, "a") as zf:
        zf.writestr("xl/vbaProject.bin", b"fake")
    with pytest.raises(IngestError) as exc:
        process_upload(FakeJob(), session, p)
    assert exc.value.kind == "macro_enabled"


def test_corrupted_xlsx_rejected(session, tmp_path):
    p = tmp_path / "book.xlsx"
    p.write_bytes(b"PK\x03\x04garbagegarbage")
    with pytest.raises(IngestError) as exc:
        process_upload(FakeJob(), session, p)
    assert exc.value.kind in {"invalid_file", "macro_enabled"}
