"""Tests for actions/smb_access.py's local file-I/O helpers (_browse,
_copy_file, _publish_file) against a plain temp directory -- these are
OS/protocol agnostic (a real UNC/SMB mount is just another Path to
them), so this covers the same logic verified by hand against a real
share (Windows' loopback C$ admin share -- see docs/README.md
"smb_access") without needing one. The Windows `net use` / Linux
`mount.cifs` branches in execute() itself aren't exercised here since
they need a real OS mechanism -- see the module docstring for their own
testing status."""

import pytest

from actions.smb_access import _browse, _copy_file, _hash_file, _publish_file


@pytest.fixture
def share(tmp_path):
    (tmp_path / "budget_q3.txt").write_text("finance data placeholder")
    (tmp_path / "notes.txt").write_text("another test file")
    (tmp_path / "subdir").mkdir()
    return tmp_path


def test_browse_lists_all_entries_including_dirs(share):
    assert set(_browse(share)) == {"budget_q3.txt", "notes.txt", "subdir"}


def test_copy_file_specific_filename(share, tmp_path):
    dest = tmp_path / "downloads"
    result = _copy_file(share, dest, "notes.txt")

    assert result["source_file"] == "notes.txt"
    assert (dest / "notes.txt").read_text() == "another test file"
    assert result["bytes_copied"] == len("another test file")
    assert result["file_hash"] == _hash_file(dest / "notes.txt")


def test_copy_file_no_filename_picks_a_file_not_the_subdir(share, tmp_path):
    result = _copy_file(share, tmp_path / "downloads", None)
    assert result["source_file"] in {"budget_q3.txt", "notes.txt"}


def test_copy_file_missing_requested_filename_raises(share, tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        _copy_file(share, tmp_path / "downloads", "does_not_exist.txt")


def test_copy_file_empty_share_raises(tmp_path):
    empty_share = tmp_path / "empty"
    empty_share.mkdir()
    with pytest.raises(RuntimeError, match="no files found"):
        _copy_file(empty_share, tmp_path / "downloads", None)


def test_copy_file_creates_dest_dir_if_missing(share, tmp_path):
    dest = tmp_path / "nested" / "downloads"
    _copy_file(share, dest, "notes.txt")
    assert dest.exists()


# --- _publish_file / _require_bare_filename -----------------------------


@pytest.fixture
def local_source(tmp_path):
    source_dir = tmp_path / "smb_downloads"
    source_dir.mkdir()
    (source_dir / "handoff.txt").write_text("directorate handoff placeholder")
    return source_dir


def test_publish_file_uploads_to_the_share(local_source, tmp_path):
    dest_share = tmp_path / "share"
    dest_share.mkdir()

    result = _publish_file(dest_share, local_source, "handoff.txt")

    assert result["published_file"] == "handoff.txt"
    assert (dest_share / "handoff.txt").read_text() == "directorate handoff placeholder"
    assert result["bytes_published"] == len("directorate handoff placeholder")
    assert result["file_hash"] == _hash_file(dest_share / "handoff.txt")


def test_publish_file_missing_local_file_raises(local_source, tmp_path):
    dest_share = tmp_path / "share"
    dest_share.mkdir()
    with pytest.raises(RuntimeError, match="not found"):
        _publish_file(dest_share, local_source, "does_not_exist.txt")


@pytest.mark.parametrize("bad_name", ["../etc/passwd", "sub/dir/file.txt", "..", ".", ""])
def test_publish_file_rejects_path_components(local_source, tmp_path, bad_name):
    # Regression guard: unlike copy_file (safe by construction -- it only
    # ever matches an already-listed iterdir() entry), publish_file
    # builds its destination directly from the given name, so a
    # traversal name must be rejected before any Path join happens.
    dest_share = tmp_path / "share"
    dest_share.mkdir()
    with pytest.raises(ValueError, match="bare name"):
        _publish_file(dest_share, local_source, bad_name)


# --- robustness fixes: subdirectory-tolerant enumeration + transient retry ---

from pathlib import Path

from actions import smb_access
from actions.smb_access import _safe_is_file, execute


def test_safe_is_file_swallows_oserror():
    """is_file() on a subdir entry over SMB can raise WinError 59; the safe
    wrapper must treat that as 'not a copyable file', never propagate."""
    class Boom:
        def is_file(self):
            raise OSError("simulated WinError 59")
    assert _safe_is_file(Boom()) is False


def test_copy_file_survives_unstattable_subdir(share, tmp_path, monkeypatch):
    """A share containing a subdir whose is_file() raises (the real-SMB
    failure mode) must not abort the copy -- the root file is still found."""
    real_is_file = Path.is_file

    def flaky(self):
        if self.name == "subdir":
            raise OSError("simulated WinError 59")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", flaky)
    result = _copy_file(share, tmp_path / "dl", "notes.txt")
    assert result["source_file"] == "notes.txt"


def test_copy_file_no_filename_skips_unstattable_subdir(share, tmp_path, monkeypatch):
    real_is_file = Path.is_file

    def flaky(self):
        if self.name == "subdir":
            raise OSError("simulated WinError 59")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", flaky)
    result = _copy_file(share, tmp_path / "dl", None)
    assert result["source_file"] in {"budget_q3.txt", "notes.txt"}


def test_execute_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_once(params, config):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("An unexpected network error occurred")  # transient
        return {"ok": True}

    monkeypatch.setattr(smb_access, "_execute_once", fake_once)
    monkeypatch.setattr(smb_access.time, "sleep", lambda *_: None)
    assert execute({}, {}) == {"ok": True}
    assert calls["n"] == 2


def test_execute_does_not_retry_nontransient(monkeypatch):
    calls = {"n": 0}

    def fake_once(params, config):
        calls["n"] += 1
        raise ValueError("bad params")

    monkeypatch.setattr(smb_access, "_execute_once", fake_once)
    monkeypatch.setattr(smb_access.time, "sleep", lambda *_: None)
    with pytest.raises(ValueError):
        execute({}, {})
    assert calls["n"] == 1  # not retried
