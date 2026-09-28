"""
smb_access: browse a real SMB (or NFS, on Linux) share, copy a file down
from it, and/or publish a local file up to it, so the OS itself
generates a real SMB session and the file server logs a real access
record, rather than faking file activity. Both directions use
local_copy_dir: copy_file writes there, publish_file reads the named
file from there -- symmetric, so a "pull from one directorate's share,
push to another's" step just needs two smb_access actions with
different `share`/`shares_category` and the same `file`.

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
    local_copy_dir          where copy_file writes the copied file
                            locally, and where publish_file reads the
                            local file it uploads from (default
                            "./smb_downloads")
    mount_point             Linux only: scratch mountpoint directory
                            (default "/mnt/cybersim_smb")
    net_use_timeout_seconds Windows only, default 15

params.file for publish_file must be a bare filename already present
under local_copy_dir -- no path separators or "..", same traversal
guard as copy_file's requested filename (there, safety comes from only
ever matching against iterdir() results; here, since the destination
path is built from the name directly, it's checked explicitly).
"""

from __future__ import annotations

import hashlib
import ntpath
import platform
import shutil
import subprocess
import time
from pathlib import Path


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Windows/CIFS "transient" network errors worth retrying rather than failing
# the whole action on. 59 = ERROR_UNEXP_NET_ERR (the one seen most in the
# ledger), 53/51/64/121 = net path/host/name/timeout classes.
_TRANSIENT_WINERRORS = {51, 53, 59, 64, 121}
_SMB_RETRIES = 2  # total attempts = _SMB_RETRIES + 1
_SMB_RETRY_BACKOFF_SECONDS = 1.5


def _is_transient_smb_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror in _TRANSIENT_WINERRORS:
        return True
    # smbprotocol raises its own error types; match on the message as a
    # last resort so a transient userspace-SMB blip is retried too.
    return "unexpected network error" in str(exc).lower()


def _safe_is_file(entry) -> bool:
    """`entry.is_file()` but never raises. Over SMB, calling is_file() (a
    stat) on a *subdirectory* entry can throw WinError 59 and abort the
    whole share enumeration -- real shares have subfolders, so a plain
    `[p for p in iterdir() if p.is_file()]` fails ~any share that isn't
    flat. Treat an entry we can't stat as "not a regular file we can copy"
    and skip it, rather than letting one bad entry kill the action."""
    try:
        return entry.is_file()
    except OSError:
        return False


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


# mount.cifs/umount against an unreachable or wedged server can block the
# agent's (single-threaded) action loop indefinitely -- a hung mount here is a
# prime cause of a host going silent, because the action never completes and
# the server then treats the host as permanently "busy". Bound both.
_MOUNT_TIMEOUT_SECONDS = 20


def _linux_mount(share: str, mount_point: Path, username: str | None, password: str | None) -> None:
    mount_point.mkdir(parents=True, exist_ok=True)
    options = "guest" if not username else f"username={username},password={password or ''}"
    subprocess.run(
        ["mount", "-t", "cifs", share, str(mount_point), "-o", options],
        capture_output=True,
        text=True,
        check=True,
        timeout=_MOUNT_TIMEOUT_SECONDS,
    )


def _linux_unmount(mount_point: Path) -> None:
    try:
        subprocess.run(
            ["umount", str(mount_point)], capture_output=True, text=True,
            timeout=_MOUNT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # A stuck mount can wedge a clean umount; a lazy detach frees the
        # mountpoint so the next run isn't blocked by the leftover.
        subprocess.run(["umount", "-l", str(mount_point)], capture_output=True, text=True)


def _browse(share_path: Path) -> list[str]:
    return [p.name for p in share_path.iterdir()]


def _require_bare_filename(filename: str) -> str:
    """publish_file builds its destination path directly from the given
    name (share_path / filename), unlike copy_file's read side, which is
    safe by construction (it only ever matches an already-listed
    iterdir() entry). A name with a path separator or '..' would let
    publish_file write outside the share -- reject it outright rather
    than relying on iterdir() to catch it after the fact."""
    if not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ValueError(f"filename must be a bare name with no path components: {filename!r}")
    return filename


def _publish_file(share_path: Path, source_dir: Path, filename: str) -> dict:
    filename = _require_bare_filename(filename)
    source = source_dir / filename
    if not source.is_file():
        raise RuntimeError(f"local file '{filename}' not found under {source_dir}")

    dest = share_path / filename
    shutil.copy2(source, dest)
    return {
        "published_file": filename,
        "dest_path": str(dest),
        "bytes_published": dest.stat().st_size,
        "file_hash": _hash_file(dest),
    }


def _copy_file(share_path: Path, dest_dir: Path, filename: str | None) -> dict:
    files = [p for p in share_path.iterdir() if _safe_is_file(p)]
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


def _userspace_smb(params: dict, config: dict) -> dict:
    """Real SMB2/3 without mount privileges; explicitly selected on Linux."""
    import smbclient

    share = params['share'].replace('/', '\\')
    parts = [part for part in share.split('\\') if part]
    if not share.startswith('\\\\') or len(parts) < 2 or any(part in ('.', '..') for part in parts):
        raise ValueError('SMB share must be a UNC server/share path without traversal')
    server = parts[0]
    share = '\\\\' + '\\'.join(parts)
    if not config.get('username'):
        raise ValueError('Userspace SMB requires explicit persona credentials')
    smbclient.register_session(server, username=config['username'],
                               password=config.get('password', ''),
                               connection_timeout=config.get('timeout_seconds', 15))
    try:
        result = {'share': params['share'], 'ops': params.get('ops', []), 'backend': 'smbprotocol'}
        if 'browse' in result['ops']:
            result['listing'] = smbclient.listdir(share)
        duration = params.get('duration_seconds', 0)
        if duration:
            time.sleep(duration)
        if 'copy_file' in result['ops']:
            files = [entry.name for entry in smbclient.scandir(share) if _safe_is_file(entry)]
            filename = params.get('file') or (files[0] if files else None)
            if filename not in files or ntpath.basename(filename) != filename or filename in ('.', '..'):
                raise ValueError('Requested file must exist directly in the share')
            destination = Path(config.get('local_copy_dir', './smb_downloads'))
            destination.mkdir(parents=True, exist_ok=True)
            destination = destination / filename
            with smbclient.open_file(ntpath.join(share, filename), mode='rb') as remote:
                with destination.open('wb') as local:
                    shutil.copyfileobj(remote, local)
            result.update(source_file=filename, dest_path=str(destination),
                          bytes_copied=destination.stat().st_size, file_hash=_hash_file(destination))
        if 'publish_file' in result['ops']:
            if not params.get('file'):
                raise ValueError('publish_file requires params.file naming the local file to upload')
            filename = _require_bare_filename(params['file'])
            source = Path(config.get('local_copy_dir', './smb_downloads')) / filename
            if not source.is_file():
                raise RuntimeError(f"local file '{filename}' not found under {source.parent}")
            with source.open('rb') as local:
                with smbclient.open_file(ntpath.join(share, filename), mode='wb') as remote:
                    shutil.copyfileobj(local, remote)
            result.update(published_file=filename, dest_path=ntpath.join(share, filename),
                          bytes_published=source.stat().st_size, file_hash=_hash_file(source))
        return result
    finally:
        smbclient.delete_session(server)


def execute(params: dict, config: dict | None = None) -> dict:
    """Run one smb_access action, retrying on transient network errors.

    A single flaky SMB round-trip (WinError 59 and friends) previously
    failed the whole action; real fileservers produce these intermittently,
    so retry a couple of times with backoff before giving up. Non-transient
    errors (auth, genuinely-missing file, bad params) still fail fast."""
    last_exc: BaseException | None = None
    for attempt in range(_SMB_RETRIES + 1):
        try:
            return _execute_once(params, config)
        except Exception as exc:  # noqa: BLE001 -- decide retry vs re-raise below
            last_exc = exc
            if attempt < _SMB_RETRIES and _is_transient_smb_error(exc):
                time.sleep(_SMB_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
    assert last_exc is not None  # unreachable; loop either returns or raises
    raise last_exc


def _execute_once(params: dict, config: dict | None = None) -> dict:
    smb_cfg = (config or {}).get("smb", {})
    if platform.system() != 'Windows' and smb_cfg.get('backend') == 'smbprotocol':
        return _userspace_smb(params, smb_cfg)
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

        if "publish_file" in ops:
            if not requested_file:
                raise ValueError("publish_file requires params.file naming the local file to upload")
            side_effects.update(_publish_file(share_path, local_copy_dir, requested_file))

        return side_effects
    finally:
        if is_windows and established_session:
            _windows_net_use_delete(share)
        elif mount_point is not None:
            _linux_unmount(mount_point)
