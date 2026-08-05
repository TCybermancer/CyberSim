"""
Remote agent install: given a target host's IP and OS, plus credentials
configured once in Settings -> Remote Install, logs into that host and
triggers the *same* install flow a human would do by hand from
/ui/install.html -- it doesn't push installer bytes over SSH/WinRM
itself. Instead it runs a short remote command that has the target host
pull its own install bundle from this server (a plain curl/
Invoke-WebRequest against GET /install/agent-bundle, authenticated by a
short-lived one-time install_token -- see app.py's remote_install route
and require_dashboard_session middleware) and run the installer.

Why pull-from-target rather than push-from-server: the Windows installer
alone is ~50MB, and WinRM's SOAP/XML transport handles bulk file
transfer poorly (base64 chunking over an XML envelope). A plain HTTP GET
initiated by the target is faster, simpler, and reuses 100% of the
already-tested bundle-generation and installer-script logic -- the only
new thing crossing the SSH/WinRM channel is a short shell/PowerShell
command.

Both install_linux/install_windows are synchronous (paramiko/pywinrm are
both blocking) -- callers run them via asyncio.to_thread (see app.py).
"""

from __future__ import annotations

import io

import paramiko
import winrm


class RemoteInstallError(Exception):
    """Connection/auth failure reaching the target host, distinct from
    the target host reaching but the install command itself failing
    (that's reported via exit_code in the returned dict, not raised)."""


_SSH_KEY_CLASS_NAMES = ("Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey")


def _load_ssh_private_key(pem: str) -> paramiko.PKey:
    """Try each key type paramiko supports in turn -- there's no single
    "detect the type" loader that covers all of them across paramiko
    versions, and the PEM header alone isn't always reliable enough to
    branch on by hand. Looked up by name via getattr rather than a
    hardcoded class list: paramiko 5.x dropped DSSKey entirely (DSA keys
    are deprecated/disabled-by-default in OpenSSH anyway) -- caught this
    the hard way (AttributeError, not a clean parse failure) with 5.0
    installed locally, so this needs to tolerate that class not existing
    on whatever paramiko version actually ends up installed."""
    key_classes = [cls for name in _SSH_KEY_CLASS_NAMES if (cls := getattr(paramiko, name, None))]
    last_error: Exception | None = None
    for key_class in key_classes:
        try:
            return key_class.from_private_key(io.StringIO(pem))
        except Exception as e:  # noqa: BLE001 -- deliberately broad, trying each type
            last_error = e
    raise RemoteInstallError(f"couldn't parse SSH private key (tried all supported types): {last_error}")


def install_linux(ip: str, ssh_user: str, ssh_private_key_pem: str, download_url: str) -> dict:
    """SSH to `ip` and run a one-liner: curl the install bundle from
    `download_url`, extract it, run install-linux.sh --silent. Returns
    {exit_code, stdout, stderr}; a non-zero exit_code means the install
    itself failed on the target (bad curl, tar, or install.sh error),
    still a "the call succeeded" result from this function's
    perspective -- only connection/auth failures raise
    RemoteInstallError."""
    try:
        key = _load_ssh_private_key(ssh_private_key_pem)
    except RemoteInstallError:
        raise
    client = paramiko.SSHClient()
    # Lab/range network, not a host we have any prior trust relationship
    # with to check a known_hosts entry against -- see docs/README.md's
    # OOB network separation section for why this is an acceptable
    # tradeoff here (this traffic never leaves the OOB management net).
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=ssh_user, pkey=key, timeout=15)
    except Exception as e:  # noqa: BLE001
        raise RemoteInstallError(f"couldn't SSH to {ip}: {e}") from e

    command = (
        "set -e; "
        "curl -fsSL '" + download_url + "' -o /tmp/cybersim-bundle.tar.gz && "
        "rm -rf /tmp/cybersim-install && mkdir -p /tmp/cybersim-install && "
        "tar xzf /tmp/cybersim-bundle.tar.gz -C /tmp/cybersim-install && "
        "/tmp/cybersim-install/install-linux.sh --silent"
    )
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=180)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
    finally:
        client.close()

    return {"exit_code": exit_code, "stdout": out, "stderr": err}


def install_windows(ip: str, winrm_user: str, winrm_password: str, download_url: str) -> dict:
    """WinRM (NTLM, HTTPS/5986, matching provisioning/inventory.ini.
    example's existing convention) to `ip` and run a PowerShell one-
    liner: download the install bundle from `download_url`, expand it,
    run cybersim-agent-setup.exe /VERYSILENT. Same success/failure
    split as install_linux: non-zero exit_code is a target-side install
    failure, not an exception."""
    session = winrm.Session(
        f"https://{ip}:5986/wsman",
        auth=(winrm_user, winrm_password),
        transport="ntlm",
        server_cert_validation="ignore",  # self-signed/no cert on a lab host is expected
        operation_timeout_sec=170,
        read_timeout_sec=180,
    )
    ps_script = f"""
$ErrorActionPreference = 'Stop'
Invoke-WebRequest -UseBasicParsing -Uri '{download_url}' -OutFile "$env:TEMP\\cybersim-bundle.zip"
Expand-Archive -Path "$env:TEMP\\cybersim-bundle.zip" -DestinationPath "$env:TEMP\\cybersim-install" -Force
& "$env:TEMP\\cybersim-install\\cybersim-agent-setup.exe" /VERYSILENT
exit $LASTEXITCODE
""".strip()

    try:
        result = session.run_ps(ps_script)
    except Exception as e:  # noqa: BLE001
        raise RemoteInstallError(f"couldn't reach {ip} over WinRM: {e}") from e

    return {
        "exit_code": result.status_code,
        "stdout": result.std_out.decode(errors="replace"),
        "stderr": result.std_err.decode(errors="replace"),
    }
