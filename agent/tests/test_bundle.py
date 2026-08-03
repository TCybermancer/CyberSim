"""Tests for actions/_bundle.py's resource_path() -- must resolve
correctly both running from source and (simulated) frozen into a
PyInstaller executable, since that's the entire reason this module
exists (see its docstring, and docs/README.md's office_doc/email_send
notes on the real bug this was built to avoid)."""

import sys
from pathlib import Path

from actions._bundle import resource_path


def test_dev_mode_resolves_relative_to_agent_dir():
    result = resource_path("templates")
    assert result == Path(__file__).resolve().parent.parent / "templates"
    assert result.is_dir()


def test_dev_mode_finds_real_template_files():
    assert resource_path("templates", "generic.txt").exists()


def test_dev_mode_finds_real_uno_worker_script():
    assert resource_path("actions", "_uno_worker.py").exists()


def test_frozen_mode_resolves_relative_to_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    result = resource_path("actions", "_uno_worker.py")
    assert result == tmp_path / "actions" / "_uno_worker.py"
