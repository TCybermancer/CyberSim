"""
Provider-agnostic live content generation for realistic scenario content
(email bodies, and eventually document content) -- called server-side,
at run-launch time, never from an agent. Agents deliberately never make
outbound calls beyond the OOB control-plane connection to this server
(see agent.py's bound_session()); giving each puppet host its own path
to an LLM API would poke a new hole through that model for every host
instead of just this one process, and would blur ground truth (is that
outbound call the simulated user, or the agent's own plumbing?).

Only reached when settings.network_mode == "connected" (see db.py's
settings table, app.py's /settings endpoints) -- airgapped deployments
never import/call into this module's network path at all. Three
providers, two request shapes:
  - anthropic: Messages API (api.anthropic.com)
  - openai: Chat Completions API (api.openai.com)
  - local: same Chat-Completions shape, against a self-hosted
    OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, ...) -- keeps
    generated content from ever leaving your own network even in
    "connected" mode.

A stored API key in settings is a fallback; CYBERSIM_<PROVIDER>_API_KEY
env vars take precedence, same pattern as CYBERSIM_ADMIN_PASSWORD, so a
real deployment doesn't have to keep a live key sitting in the DB.

No SDK dependencies -- these are simple, well-documented JSON REST
calls, and hand-rolling them with `requests` (already needed here
regardless) avoids pulling in and version-pinning three separate
provider SDKs for what's fundamentally one HTTP POST each.
"""

from __future__ import annotations

import os

import requests

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
}

_TIMEOUT_SECONDS = 30
_MAX_TOKENS = 600


class ContentGenError(Exception):
    """Raised on any failure to reach/parse a provider response. Callers
    (see scenario_engine.py) are expected to catch this and fall back to
    the static template library rather than let a launch fail outright
    over a flaky API call."""


def _resolve_key(settings: dict, provider: str) -> str | None:
    env_key = os.environ.get(f"CYBERSIM_{provider.upper()}_API_KEY")
    if env_key:
        return env_key
    return settings.get(f"{provider}_api_key")


def _call_anthropic(settings: dict, system_prompt: str, user_prompt: str) -> str:
    api_key = _resolve_key(settings, "anthropic")
    if not api_key:
        raise ContentGenError(
            "no Anthropic API key configured (set it in Settings, or CYBERSIM_ANTHROPIC_API_KEY)"
        )
    model = settings.get("anthropic_model") or DEFAULT_MODELS["anthropic"]

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        raise ContentGenError(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return "".join(block["text"] for block in data["content"] if block.get("type") == "text").strip()
    except (KeyError, TypeError) as e:
        raise ContentGenError(f"unexpected Anthropic response shape: {e}")


def _call_openai_compatible(
    base_url: str, api_key: str | None, model: str, system_prompt: str, user_prompt: str, provider_label: str
) -> str:
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json={
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        raise ContentGenError(f"{provider_label} API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise ContentGenError(f"unexpected {provider_label} response shape: {e}")


def _call_openai(settings: dict, system_prompt: str, user_prompt: str) -> str:
    api_key = _resolve_key(settings, "openai")
    if not api_key:
        raise ContentGenError("no OpenAI API key configured (set it in Settings, or CYBERSIM_OPENAI_API_KEY)")
    model = settings.get("openai_model") or DEFAULT_MODELS["openai"]
    return _call_openai_compatible(
        "https://api.openai.com/v1", api_key, model, system_prompt, user_prompt, "OpenAI"
    )


def _call_local(settings: dict, system_prompt: str, user_prompt: str) -> str:
    base_url = settings.get("local_base_url")
    if not base_url:
        raise ContentGenError("no local endpoint base URL configured in Settings")
    model = settings.get("local_model") or "local-model"
    api_key = _resolve_key(settings, "local")
    return _call_openai_compatible(base_url, api_key, model, system_prompt, user_prompt, "local")


_PROVIDERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "local": _call_local,
}


def generate_content(settings: dict, system_prompt: str, user_prompt: str) -> str:
    """settings is db.get_settings()'s dict. Raises ContentGenError on
    any failure -- callers decide the fallback (see scenario_engine.py),
    this module never silently returns empty/placeholder content."""
    provider = settings.get("llm_provider", "anthropic")
    handler = _PROVIDERS.get(provider)
    if not handler:
        raise ContentGenError(f"unknown llm_provider '{provider}'")
    try:
        return handler(settings, system_prompt, user_prompt)
    except requests.RequestException as e:
        raise ContentGenError(f"{provider} request failed: {e}")
