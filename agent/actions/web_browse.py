"""
web_browse: drive a real, persistent-profile browser to a target URL and
interact with it for the scenario's duration.

Uses Playwright's sync API, so this stays a drop-in synchronous
`execute(params, config) -> dict` like every other action module --
agent.py runs one action at a time per host, so there's no need for
async here.

Local setup:
    pip install playwright
    playwright install chromium

Packaged (PyInstaller) note: PyInstaller's Playwright hook points browser
lookup at a path relative to the frozen driver's own extraction dir by
default -- harmless for `--onedir` builds, but fatal for `--onefile`
(which extracts to a fresh temp dir every launch, so a browser "found"
once would need re-downloading on every subsequent run). The
PLAYWRIGHT_BROWSERS_PATH override below forces Playwright back to its
normal (unfrozen) shared per-user cache regardless, so `playwright
install chromium` run once against that cache is actually found on every
launch.

Config (agent config.yaml, `browser:` block):
    profile_dir   base directory for per-persona persistent profiles
                  (default "./browser_profiles"). Cookies/history
                  accumulate here across actions and runs, like a real
                  user's browser -- keyed by config['persona'], since one
                  agent host represents one simulated user for its whole
                  lifetime.
    headless      default false -- a real user's browser isn't headless,
                  and some detection tooling fingerprints headless
                  Chromium. Set true only for CI/dev where nothing needs
                  to actually see the window.
    channel       Playwright browser channel, e.g. "chrome" to drive a
                  real installed Chrome instead of bundled Chromium;
                  omit to use bundled Chromium.

Returns observed_side_effects (final URL, HTTP status, page title, and
the profile directory used) that are independently verifiable by
grepping the browser's real history/profile after the fact.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path


def _ensure_playwright_browsers_path() -> None:
    """Must run before Playwright is imported -- see module docstring.
    Respects an operator-set override; otherwise points at Playwright's
    own normal per-OS shared cache location."""
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        return
    if os.name == "nt":
        default = Path(os.environ["USERPROFILE"]) / "AppData" / "Local" / "ms-playwright"
    elif sys.platform == "darwin":
        default = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        default = Path.home() / ".cache" / "ms-playwright"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(default)


_ensure_playwright_browsers_path()

from playwright.sync_api import sync_playwright  # noqa: E402 -- must follow the env var fix above


def _browse(page, deadline: float) -> None:
    """Best-effort scroll/click traversal until deadline. A dead link,
    an off-page navigation, or a click timeout shouldn't fail the whole
    action -- only the initial navigation in execute() is load-bearing."""
    while time.monotonic() < deadline:
        try:
            page.mouse.wheel(0, random.randint(200, 800))
            links = page.locator("a[href]")
            if links.count() and random.random() < 0.4:
                links.nth(random.randrange(links.count())).click(timeout=2000)
                page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        time.sleep(min(max(deadline - time.monotonic(), 0), random.uniform(3, 8)))


def execute(params: dict, config: dict | None = None) -> dict:
    config = config or {}
    browser_cfg = config.get("browser", {})
    persona = config.get("persona") or "default"

    profile_dir = Path(browser_cfg.get("profile_dir", "./browser_profiles")) / persona
    profile_dir.mkdir(parents=True, exist_ok=True)

    target = params["target"]
    duration = params.get("duration_seconds", 0)

    launch_kwargs = {"headless": browser_cfg.get("headless", False)}
    if browser_cfg.get("channel"):
        launch_kwargs["channel"] = browser_cfg["channel"]

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            response = page.goto(target, wait_until="load")

            _browse(page, deadline=time.monotonic() + duration)

            return {
                "target": target,
                "final_url": page.url,
                "http_status": response.status if response else None,
                "page_title": page.title(),
                "profile_dir": str(profile_dir),
            }
        finally:
            context.close()
