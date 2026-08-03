"""Tests for actions/smb_access.py's local file-I/O helpers (_browse,
_copy_file) against a plain temp directory -- these are OS/protocol
agnostic (a real UNC/SMB mount is just another Path to them), so this
covers the same logic verified by hand against a real share (Windows'
loopback C$ admin share -- see docs/README.md "smb_access") without
needing one. The Windows `net use` / Linux `mount.cifs` branches in
execute() itself aren't exercised here since they need a real OS
mechanism -- see the module docstring for their own testing status."""

import pytest

from actions.smb_access import _browse, _copy_file, _hash_file


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
