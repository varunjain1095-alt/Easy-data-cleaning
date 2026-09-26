import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sessions import SessionManager  # noqa: E402


@pytest.fixture
def session(tmp_path):
    mgr = SessionManager(tmp_path / "sessions", ttl=3600)
    return mgr.create()


@pytest.fixture
def registry(tmp_path):
    from app.jobs import JobRegistry

    return JobRegistry(tmp_path / "sessions", workers=2)
