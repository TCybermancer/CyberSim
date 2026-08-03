"""
smb_access: browse a real SMB (or NFS, on Linux) share and copy a file,
so the OS itself generates a real SMB session and the file server logs a
real access record, rather than faking file activity.

Windows: UNC paths (\\\\server\\share\\...) are directly usable via
Python's os/shutil once reachable -- no drive-letter mapping needed for
plain read access. If config supplies credentials, `net use` establishes
an authenticated session first (and tears it down after via `net use
/delete`), since the account running the agent may not already have
access to the share.

Linux: mounts the share via mount.cifs (requires root/CAP_SYS_ADMIN --
grant the agent that specific capability rather than running the whole
process as root) at a scratch mountpoint, does real file I/O against the
mount, then unmounts. Written against the documented mount.cifs
interface but not exercised on the Windows sandbox this was built on --
verify on a real Linux puppet host before trusting it.

Config (agent config.yaml, `smb:` block):
    username / password    optional; if set, authenticates the session
                            before accessing the share
    local_copy_dir          where a copy_file op writes the copied file
                            locally (default "./smb_downloads")
    mount_point             Linux only: scratch mountpoint directory
                            (default "/mnt/cybersim_smb")
    net_use_timeout_seconds Windows only, default 15
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import time
from pathlib import Path


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _windows_net_use(share: str, username: str | None, password: str | None, timeout: int) -> bool:
    """Establishes an authenticated session via `net use` if credentials
    were given. Returns whether it did, so execute() knows whether to
    tear the session down afterward -- accessing a share the agent's
    existing token already has rights to needs no explicit session at
    all, and shouldn't have one torn down that it didn't create."""
    if not username:
        return False
    subprocess.run(
        ["net", "use", share, password or "", f"/user:{username}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return True


def _windows_net_use_delete(share: str) -> None:
    subprocess.run(["net", "use", share, "/delete", "/y"], capture_output=True, text=True)


def _linux_mount(share: str, mount_point: Path, username: str | None, password: str | None) -> None:
    mount_point.mkdir(parents=True, exist_ok=True)
    options = "guest" if not username else f"username={username},password={password or ''}"
    subprocess.run(
        ["mount", "-t", "cifs", share, str(mount_point), "-o", options],
        capture_output=True,
        text=True,
        check=True,
    )


def _linux_unmount(mount_point: Path) -> None:
    subprocess.run(["umount", str(mount_point)], capture_output=True, text=True)


def _browse(share_path: Path) -> list[str]:
    return [p.name for p in share_path.iterdir()]


def _copy_file(share_path: Path, dest_dir: Path, filename: str | None) -> dict:
    files = [p for p in share_path.iterdir() if p.is_file()]
    if filename:
        source = next((p for p in files if p.name == filename), None)
        if source is None:
            raise RuntimeError(f"requested file '{filename}' not found under {share_path}")
    elif files:
        source = files[0]
    else:
        raise RuntimeError(f"no files found under {share_path} to copy")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    shutil.copy2(source, dest)
    return {
        "source_file": source.name,
        "dest_path": str(dest),
        "bytes_copied": dest.stat().st_size,
        "file_hash": _hash_file(dest),
    }


def execute(params: dict, config: dict | None = None) -> dict:
    smb_cfg = (config or {}).get("smb", {})
    username = smb_cfg.get("username")
    password = smb_cfg.get("password")
    local_copy_dir = Path(smb_cfg.get("local_copy_dir", "./smb_downloads"))

    share = params["share"]
    ops = params.get("ops", [])
    requested_file = params.get("file")
    duration = params.get("duration_seconds", 0)

    is_windows = platform.system() == "Windows"
    established_session = False
    mount_point: Path | None = None

    try:
        if is_windows:
            established_session = _windows_net_use(
                share, username, password, smb_cfg.get("net_use_timeout_seconds", 15)
            )
            share_path = Path(share)
        else:
            mount_point = Path(smb_cfg.get("mount_point", "/mnt/cybersim_smb"))
            _linux_mount(share, mount_point, username, password)
            share_path = mount_point

        side_effects: dict = {"share": share, "ops": ops}

        if "browse" in ops:
            side_effects["listing"] = _browse(share_path)

        if duration:
            time.sleep(duration)  # dwell time browsing, like a real user pausing to read filenames

        if "copy_file" in ops:
            side_effects.update(_copy_file(share_path, local_copy_dir, requested_file))

        return side_effects
    finally:
        if is_windows and established_session:
            _windows_net_use_delete(share)
        elif mount_point is not None:
            _linux_unmount(mount_point)
