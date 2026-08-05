"""Tests for content_gen.py's provider dispatch. All network calls are
mocked (unittest.mock) -- these never hit a real API."""

from unittest.mock import MagicMock, patch

import pytest

import content_gen


def _settings(**overrides):
    base = {
        "network_mode": "connected",
        "llm_provider": "anthropic",
        "anthropic_api_key": None,
        "anthropic_model": None,
        "openai_api_key": None,
        "openai_model": None,
        "local_base_url": None,
        "local_api_key": None,
        "local_model": None,
    }
    base.update(overrides)
    return base


def _mock_response(status_ok=True, status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.ok = status_ok
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


@patch("content_gen.requests.post")
def test_anthropic_success(mock_post):
    mock_post.return_value = _mock_response(
        json_body={"content": [{"type": "text", "text": "Hello from Claude"}]}
    )
    settings = _settings(anthropic_api_key="sk-ant-test")

    result = content_gen.generate_content(settings, "system prompt", "user prompt")

    assert result == "Hello from Claude"
    call = mock_post.call_args
    assert call.args[0] == "https://api.anthropic.com/v1/messages"
    assert call.kwargs["headers"]["x-api-key"] == "sk-ant-test"
    assert call.kwargs["json"]["model"] == content_gen.DEFAULT_MODELS["anthropic"]


@patch("content_gen.requests.post")
def test_anthropic_uses_custom_model(mock_post):
    mock_post.return_value = _mock_response(json_body={"content": [{"type": "text", "text": "hi"}]})
    settings = _settings(anthropic_api_key="sk-ant-test", anthropic_model="claude-custom")

    content_gen.generate_content(settings, "sys", "user")

    assert mock_post.call_args.kwargs["json"]["model"] == "claude-custom"


def test_anthropic_missing_key_raises():
    settings = _settings(anthropic_api_key=None)
    with pytest.raises(content_gen.ContentGenError, match="no Anthropic API key"):
        content_gen.generate_content(settings, "sys", "user")


@patch("content_gen.requests.post")
def test_anthropic_api_error_raises(mock_post):
    mock_post.return_value = _mock_response(status_ok=False, status_code=401, text="unauthorized")
    settings = _settings(anthropic_api_key="bad-key")

    with pytest.raises(content_gen.ContentGenError, match="401"):
        content_gen.generate_content(settings, "sys", "user")


@patch("content_gen.requests.post")
def test_openai_success(mock_post):
    mock_post.return_value = _mock_response(
        json_body={"choices": [{"message": {"content": "Hello from GPT"}}]}
    )
    settings = _settings(llm_provider="openai", openai_api_key="sk-test")

    result = content_gen.generate_content(settings, "sys", "user")

    assert result == "Hello from GPT"
    call = mock_post.call_args
    assert call.args[0] == "https://api.openai.com/v1/chat/completions"
    assert call.kwargs["headers"]["Authorization"] == "Bearer sk-test"


@patch("content_gen.requests.post")
def test_local_success_with_custom_base_url(mock_post):
    mock_post.return_value = _mock_response(
        json_body={"choices": [{"message": {"content": "Hello from local model"}}]}
    )
    settings = _settings(
        llm_provider="local", local_base_url="http://localhost:11434/v1", local_model="llama3.1"
    )

    result = content_gen.generate_content(settings, "sys", "user")

    assert result == "Hello from local model"
    call = mock_post.call_args
    assert call.args[0] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in call.kwargs["headers"]  # no key configured -- omitted, not sent blank
    assert call.kwargs["json"]["model"] == "llama3.1"


def test_local_missing_base_url_raises():
    settings = _settings(llm_provider="local")
    with pytest.raises(content_gen.ContentGenError, match="base URL"):
        content_gen.generate_content(settings, "sys", "user")


@patch.dict("os.environ", {"CYBERSIM_ANTHROPIC_API_KEY": "env-key"})
@patch("content_gen.requests.post")
def test_env_var_key_takes_precedence_over_stored(mock_post):
    mock_post.return_value = _mock_response(json_body={"content": [{"type": "text", "text": "hi"}]})
    settings = _settings(anthropic_api_key="stored-key")

    content_gen.generate_content(settings, "sys", "user")

    assert mock_post.call_args.kwargs["headers"]["x-api-key"] == "env-key"


def test_unknown_provider_raises():
    settings = _settings(llm_provider="carrier-pigeon")
    with pytest.raises(content_gen.ContentGenError, match="unknown llm_provider"):
        content_gen.generate_content(settings, "sys", "user")
