"""
Runs under LibreOffice's OWN bundled Python interpreter (invoked as a
subprocess by office_doc.py -- see that module's docstring for why this
can't just be imported and called from the agent's normal venv).

Uses officehelper.bootstrap() -- shipped with LibreOffice itself, next to
uno.py -- to launch a headless soffice instance and connect to it, with
built-in retry/backoff. Opens (or creates) the target document, performs
the requested ops, hashes the file before/after, saves, closes, and
cleanly terminates the office process it started.

Prints a fixed marker line followed by one line of JSON so the parent
process can find the result even if pyuno/soffice logged other noise to
stdout/stderr.

Usage: python.exe _uno_worker.py '<json args>'
    args: {soffice_path, port unused, app, file_path, ops, duration_seconds}
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import officehelper
import uno
from com.sun.star.beans import PropertyValue

RESULT_MARKER = "===UNO_RESULT==="

# (UNO filter name, "new document" factory URL) per supported app.
_APP_INFO = {
    "libreoffice_calc": ("Calc MS Excel 2007 XML", "private:factory/scalc"),
    "libreoffice_writer": ("MS Word 2007 XML", "private:factory/swriter"),
}


def _prop(name: str, value) -> PropertyValue:
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def _hash_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _edit(document, app: str) -> None:
    stamp = f"cybersim edit {time.strftime('%Y-%m-%d %H:%M:%S')}"
    if app == "libreoffice_calc":
        sheet = document.Sheets.getByIndex(0)
        col = 0
        while sheet.getCellByPosition(col, 0).getString():
            col += 1
        sheet.getCellByPosition(col, 0).setString(stamp)
    else:
        text = document.getText()
        text.insertString(text.getEnd(), f"\n{stamp}\n", False)


def run(args: dict) -> dict:
    app = args.get("app", "libreoffice_calc")
    if app not in _APP_INFO:
        raise ValueError(f"unsupported app '{app}' (know: {sorted(_APP_INFO)})")
    filter_name, new_doc_url = _APP_INFO[app]

    file_path = Path(args["file_path"])
    ops = args.get("ops", [])
    duration = args.get("duration_seconds", 0)

    hash_before = _hash_file(file_path)
    existed_before = file_path.exists()

    # officehelper.bootstrap() quotes ITS OWN auto-detected soffice path
    # (to survive shell=True with spaces in "Program Files") but not a
    # caller-supplied one -- quote it ourselves or "C:\Program" gets
    # split as the command and "Files\...\soffice.exe" as an argument.
    soffice_arg = args.get("soffice_path")
    ctx = officehelper.bootstrap(soffice=f'"{soffice_arg}"' if soffice_arg else None)
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    try:
        if existed_before:
            url = uno.systemPathToFileUrl(str(file_path))
            document = desktop.loadComponentFromURL(url, "_blank", 0, (_prop("Hidden", True),))
        else:
            document = desktop.loadComponentFromURL(
                new_doc_url, "_blank", 0, (_prop("Hidden", True),)
            )

        if "edit_cells" in ops or "edit" in ops:
            _edit(document, app)

        if duration:
            time.sleep(duration)  # document stays open+dirty for the dwell, like a real user working on it

        if "save" in ops:
            if existed_before:
                document.store()
            else:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                document.storeToURL(
                    uno.systemPathToFileUrl(str(file_path)), (_prop("FilterName", filter_name),)
                )

        if "close" in ops:
            document.close(False)
    finally:
        desktop.terminate()

    return {
        "app": app,
        "file": str(file_path),
        "ops": ops,
        "file_hash_before": hash_before,
        "file_hash_after": _hash_file(file_path),
        "file_existed_before": existed_before,
    }


def main() -> None:
    try:
        side_effects = run(json.loads(sys.argv[1]))
        print(RESULT_MARKER)
        print(json.dumps({"ok": True, "side_effects": side_effects}))
    except Exception as exc:  # noqa: BLE001 -- report to parent as data, not a bare traceback
        print(RESULT_MARKER)
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
