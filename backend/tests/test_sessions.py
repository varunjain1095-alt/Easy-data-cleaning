import json
import time

from app.sessions import SessionManager


def test_create_and_get(tmp_path):
    mgr = SessionManager(tmp_path / "s", ttl=3600)
    s = mgr.create()
    assert s.token and len(s.token) > 20
    fetched = mgr.get(s.token)
    assert fetched is not None and fetched.token == s.token


def test_expired_session_is_deleted(tmp_path):
    mgr = SessionManager(tmp_path / "s", ttl=3600)
    s = mgr.create()
    meta_path = s.dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["expires_at"] = time.time() - 1
    meta_path.write_text(json.dumps(meta))
    assert mgr.get(s.token) is None
    assert not s.dir.exists()


def test_touch_extends_expiry(tmp_path):
    mgr = SessionManager(tmp_path / "s", ttl=10)
    s = mgr.create()
    before = s.expires_at
    time.sleep(0.01)
    s.touch()
    assert s.expires_at > before


def test_cleanup_expired(tmp_path):
    mgr = SessionManager(tmp_path / "s", ttl=3600)
    a = mgr.create()
    b = mgr.create()
    meta_path = a.dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["expires_at"] = time.time() - 1
    meta_path.write_text(json.dumps(meta))
    assert mgr.cleanup_expired() == 1
    assert mgr.get(b.token) is not None


def test_bad_token_rejected(tmp_path):
    mgr = SessionManager(tmp_path / "s", ttl=3600)
    assert mgr.get("../evil") is None
    assert mgr.get("") is None
    assert mgr.get("nonexistent") is None
