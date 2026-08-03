"""
office_doc: open, edit, save, and close a real LibreOffice document via
its UNO API -- produces authentic soffice.bin process activity, file
handle events, and file hash deltas (much stronger detection-tool signal
than touching the file with plain Python I/O).

Why a subprocess: LibreOffice's Python UNO bridge (pyuno.pyd on Windows,
libpyuno.so on Linux) is a compiled extension built against LibreOffice's
own bundled Python interpreter (LibreOffice/program/python[.exe]) -- it
will not import under the agent's own venv/system Python (different
build, different ABI; confirmed by hand while building this). So this
module launches the real automation (_uno_worker.py, which uses
LibreOffice's own officehelper.bootstrap() to start soffice and connect)
*as a subprocess under LibreOffice's bundled Python*, and parses its
JSON result back. This is the standard way to drive UNO from a process
that isn't itself running inside LibreOffice's interpreter.

Config (agent config.yaml, `office:` block):
    soffice_path          path to soffice(.exe); default "soffice",
                           i.e. whatever's on PATH
    bundled_python_path   path to LibreOffice's own python(.exe);
                           default derived from soffice_path's directory
    documents_dir          base directory for puppet documents (default
                           "./documents") -- a scenario's params['file']
                           is resolved relative to this, and created
                           fresh (via the app's factory document) if it
                           doesn't exist yet
    timeout_seconds        subprocess timeout margin on top of the
                           action's own duration_seconds (default 120)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ._bundle import resource_path

_WORKER = resource_path("actions", "_uno_worker.py")
_RESULT_MARKER = "===UNO_RESULT==="


def _default_bundled_python(soffice_path: Path) -> Path:
    name = "python.exe" if soffice_path.suffix.lower() == ".exe" else "python"
    return soffice_path.parent / name


def execute(params: dict, config: dict | None = None) -> dict:
    office_cfg = (config or {}).get("office", {})
    soffice_path = Path(office_cfg.get("soffice_path", "soffice"))
    bundled_python = Path(
        office_cfg.get("bundled_python_path") or _default_bundled_python(soffice_path)
    )
    documents_dir = Path(office_cfg.get("documents_dir", "./documents"))
    timeout_margin = office_cfg.get("timeout_seconds", 120)

    duration = params.get("duration_seconds", 0)
    worker_args = {
        "soffice_path": str(soffice_path),
        "app": params.get("app", "libreoffice_calc"),
        "file_path": str((documents_dir / params["file"]).resolve()),
        "ops": params.get("ops", []),
        "duration_seconds": duration,
    }

    proc = subprocess.run(
        [str(bundled_python), str(_WORKER), json.dumps(worker_args)],
        capture_output=True,
        text=True,
        timeout=duration + timeout_margin,
    )

    if _RESULT_MARKER not in proc.stdout:
        raise RuntimeError(
            f"UNO worker produced no result (exit={proc.returncode}); "
            f"stdout={proc.stdout[-2000:]!r} stderr={proc.stderr[-2000:]!r}"
        )
    result = json.loads(proc.stdout.split(_RESULT_MARKER, 1)[1].strip().splitlines()[0])
    if not result.get("ok"):
        raise RuntimeError(f"UNO worker reported failure: {result.get('error')}")
    return result["side_effects"]
