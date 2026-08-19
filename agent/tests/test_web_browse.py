"""Tests for actions/web_browse.py's PLAYWRIGHT_BROWSERS_PATH fix -- see
the module docstring for why this exists (PyInstaller onefile extracts
to a fresh temp dir every launch, so the default frozen browser-lookup
path would need re-downloading Chromium every run). Doesn't launch a
real browser -- that was verified by hand (see docs/README.md
"web_browse")."""

import os

import pytest

from actions.web_browse import (
    _ensure_playwright_browsers_path,
    _installed_windows_browser_channel,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)


def test_respects_an_existing_operator_override():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/custom/path"
    _ensure_playwright_browsers_path()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/custom/path"


def test_sets_a_default_shared_cache_path_when_unset():
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ
    _ensure_playwright_browsers_path()
    # Exact path is platform-dependent (see module docstring for the
    # three branches) -- what matters is it's set, non-empty, and points
    # at the well-known ms-playwright cache dir name, not something
    # relative to a frozen build's temp extraction dir.
    value = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    assert value
    assert value.replace("\\", "/").rstrip("/").endswith("ms-playwright")


def test_detects_installed_chrome_on_windows(monkeypatch, tmp_path):
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)

    assert _installed_windows_browser_channel() == "chrome"


def test_returns_none_when_no_system_browser_is_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)

    assert _installed_windows_browser_channel() is None
