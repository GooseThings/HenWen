"""Shared pytest fixtures for the HenWen test suite.

app.py reads all of its configuration (DB_PATH, RPT_CONF_PATH, SECRET_KEY,
etc.) from environment variables exactly once, at import time, and starts
its background AMI/network polling threads at the bottom of the module
unconditionally -- unless HENWEN_SKIP_STARTUP is set. Both of those mean the
environment has to be prepared *before* `import app` ever runs, which is why
this all happens at conftest module-import time rather than inside a
fixture.
"""
import os
import sys
import tempfile

import pytest

_TEST_ROOT = tempfile.mkdtemp(prefix="henwen-tests-")

os.environ.setdefault("HENWEN_SKIP_STARTUP", "1")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DB_PATH", os.path.join(_TEST_ROOT, "henwen.db"))
os.environ.setdefault("RPT_CONF_PATH", os.path.join(_TEST_ROOT, "rpt.conf"))
os.environ.setdefault("BACKUP_DIR", os.path.join(_TEST_ROOT, "rpt_backups"))
os.environ.setdefault("SOUNDS_DIR", os.path.join(_TEST_ROOT, "sounds"))
os.environ.setdefault("TTS_VOICES_DIR", os.path.join(_TEST_ROOT, "tts_voices"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as henwen_app  # noqa: E402

henwen_app.app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=False,      # exercising CSRF plumbing isn't the point of these tests
    RATELIMIT_ENABLED=False,     # login/api_login carry per-minute limits that would
                                  # otherwise trip across unrelated tests sharing one process
)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point the app at a brand-new, empty sqlite DB for the duration of one test.

    get_db() only runs its CREATE TABLE / migration statements once per
    process (guarded by the _db_ready flag) since it's called on nearly
    every request -- so a test that just swapped DB_PATH also has to reset
    _db_ready, or get_db() will happily hand back a connection to a file
    that was never initialized.
    """
    db_path = tmp_path / "henwen.db"
    monkeypatch.setattr(henwen_app, "DB_PATH", str(db_path))
    monkeypatch.setattr(henwen_app, "_db_ready", False)
    conn = henwen_app.get_db()
    yield conn
    conn.close()


@pytest.fixture()
def client(fresh_db):
    """Flask test client backed by an isolated DB and a clean active-session table."""
    henwen_app._active_sessions.clear()
    return henwen_app.app.test_client()


@pytest.fixture()
def create_user(fresh_db):
    """Factory fixture: create_user('alice', 'password123', role='owner') -> user row."""
    from werkzeug.security import generate_password_hash

    def _make(username, password="password12345", role="owner"):
        fresh_db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            (username, generate_password_hash(password), role),
        )
        fresh_db.commit()
        return fresh_db.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()

    return _make
