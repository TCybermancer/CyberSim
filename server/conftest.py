"""
Makes server/ importable as flat modules (import db, import app, from
models import ...) for tests under server/tests/ -- matches how the app
is actually run (cwd=server/), rather than relying on pytest's own
rootdir-insertion rules being invoked a particular way.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402

import pytest  # noqa: E402


TEST_ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway SQLite DB -- never touches
    whatever real cybersim.db might exist locally. Also pins the
    dashboard admin password to a known value (instead of the random
    one app.py's startup would otherwise generate) so tests can actually
    log in -- see test_app.py's `client` fixture."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("CYBERSIM_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    db.init_db()
    yield
