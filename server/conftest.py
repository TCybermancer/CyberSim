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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway SQLite DB -- never touches
    whatever real cybersim.db might exist locally."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield
