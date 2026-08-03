"""
Locates packaged resources -- files the code needs to run (not
user-configurable runtime state like a browser profile dir) -- in a way
that works both running from source and frozen into a PyInstaller
executable.

In a frozen onefile build, imported modules run from an in-memory
archive with no real path on disk, so a plain `Path(__file__).parent`
can't be used to find sibling non-code files. Actual files only exist on
disk if bundled as PyInstaller "datas" and extracted to sys._MEIPASS at
startup -- see ../cybersim-agent.spec, whose datas entries must use
the same relative destinations as the `*parts` passed here.
"""

from __future__ import annotations

import sys
from pathlib import Path


def resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent  # agent/
    return base.joinpath(*parts)
