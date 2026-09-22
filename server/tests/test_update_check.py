"""Tests for update_check.py. All network calls are mocked -- these
never hit the real GitHub API (see the module docstring for why the
real thing is only ever called on an explicit admin click, not here)."""

from unittest.mock import MagicMock, patch

import pytest

import update_check


def _mock_response(ok=True, status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


@pytest.mark.parametrize(
    "version,expected",
    [
        ("v0.3.0", (0, 3, 0)),
        ("0.3.0", (0, 3, 0)),
        ("V1.2.3", (1, 2, 3)),
        ("v0.3.0-rc1", (0, 3, 0)),
        ("v0.3.0+build5", (0, 3, 0)),
        ("garbage", (0,)),
    ],
)
def test_parse_version(version, expected):
    assert update_check._parse_version(version) == expected


def test_is_newer_true_for_a_later_version():
    assert update_check.is_newer("v0.3.0", "0.2.0") is True


def test_is_newer_false_for_the_same_version():
    assert update_check.is_newer("v0.2.0", "0.2.0") is False


def test_is_newer_false_for_an_older_version():
    assert update_check.is_newer("v0.1.0", "0.2.0") is False


@patch("update_check.requests.get")
def test_fetch_latest_release_success(mock_get):
    mock_get.return_value = _mock_response(
        json_body={
            "tag_name": "v0.3.0",
            "html_url": "https://github.com/TCybermancer/CyberSim/releases/tag/v0.3.0",
            "published_at": "2026-02-01T00:00:00Z",
        }
    )

    release = update_check.fetch_latest_release()

    assert release == {
        "tag": "v0.3.0",
        "url": "https://github.com/TCybermancer/CyberSim/releases/tag/v0.3.0",
        "published_at": "2026-02-01T00:00:00Z",
    }
    assert mock_get.call_args.args[0] == update_check._RELEASES_URL
    assert mock_get.call_args.kwargs["headers"]["User-Agent"]


@patch("update_check.requests.get")
def test_fetch_latest_release_wraps_non_ok_response(mock_get):
    mock_get.return_value = _mock_response(ok=False, status_code=404, text="Not Found")

    with pytest.raises(update_check.UpdateCheckError, match="404"):
        update_check.fetch_latest_release()


@patch("update_check.requests.get")
def test_fetch_latest_release_wraps_unexpected_response_shape(mock_get):
    mock_get.return_value = _mock_response(json_body={"unexpected": "shape"})

    with pytest.raises(update_check.UpdateCheckError):
        update_check.fetch_latest_release()


@patch("update_check.requests.get")
def test_fetch_latest_release_wraps_connection_errors(mock_get):
    import requests

    mock_get.side_effect = requests.ConnectionError("no route to host")

    with pytest.raises(update_check.UpdateCheckError, match="couldn't reach GitHub"):
        update_check.fetch_latest_release()
