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
import os
import json
import sys
import time
import signal
import subprocess
import tempfile
import uuid
import shutil
from pathlib import Path

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


def _owned_bootstrap(soffice_path: str, profile: str):
    pipe = 'cybersim_' + uuid.uuid4().hex
    popen_kwargs = {
        'cwd': str(Path.home()),
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
    }
    if sys.platform.startswith('win'):
        popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs['start_new_session'] = True
    process = subprocess.Popen([
        soffice_path, '-env:UserInstallation=' + Path(profile).as_uri(),
        '--headless', '--nologo', '--nodefault', '--norestore',
        '--accept=pipe,name=' + pipe + ';urp;StarOffice.ServiceManager',
    ], **popen_kwargs)
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            'com.sun.star.bridge.UnoUrlResolver', local)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('LibreOffice exited before UNO connection: ' + str(process.returncode))
            try:
                return resolver.resolve('uno:pipe,name=' + pipe + ';urp;StarOffice.ComponentContext'), process
            except uno.getClass('com.sun.star.connection.NoConnectException'):
                time.sleep(0.5)
        raise TimeoutError('LibreOffice UNO startup exceeded 60 seconds')
    except BaseException:
        _stop_owned_office(process)
        raise


def _stop_owned_office(process):
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if sys.platform.startswith('win'):
                subprocess.run(
                    ['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _cleanup_profile(profile) -> None:
    """Remove the isolated LibreOffice profile without failing the action.

    On Windows, soffice.exe can exit just before its soffice.bin child releases
    extension registry files.  TemporaryDirectory.cleanup() then raises a
    sharing-violation even though the document operation and office shutdown
    both succeeded.  Retry that short release window and leave only the
    disposable profile behind if Windows still has it locked; a cleanup race
    must not turn a completed user action into a false failure.
    """
    if profile is None:
        return
    for attempt in range(20):
        try:
            profile.cleanup()
            return
        except OSError:
            if attempt < 19:
                time.sleep(0.5)
    shutil.rmtree(profile.name, ignore_errors=True)


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

    soffice_arg = args.get("soffice_path")
    process = None
    profile = tempfile.TemporaryDirectory(prefix='cybersim-office-')
    try:
        ctx, process = _owned_bootstrap(soffice_arg or 'soffice', profile.name)
    except BaseException:
        _cleanup_profile(profile)
        raise
    desktop = None
    try:
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
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
        try:
            if desktop is not None:
                desktop.terminate()
        finally:
            if process is not None:
                _stop_owned_office(process)
            if profile is not None:
                _cleanup_profile(profile)

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
