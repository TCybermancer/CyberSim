"""Tests for remote_install.py. install_linux/install_windows themselves
aren't exercised end to end here (that needs a real reachable SSH/WinRM
target) -- see server/tests/test_app.py's remote-install tests for the
route-level behavior with these functions mocked. What's covered here is
the part that mocking them entirely would hide: the real paramiko key-
parsing path, and that a connection failure actually gets translated
into RemoteInstallError rather than propagating as a raw exception (see
app.py's remote_install_route, which only catches RemoteInstallError)."""

import io
from unittest.mock import MagicMock, patch

import paramiko
import pytest

import remote_install


@pytest.fixture(scope="module")
def rsa_private_key_pem():
    """A real, throwaway 1024-bit key generated at test time (small for
    speed -- this is never used for anything but parsing) rather than a
    hardcoded blob, partly to dodge secret-scanner false positives on a
    private-key-shaped string sitting in the repo."""
    key = paramiko.RSAKey.generate(1024)
    buf = io.StringIO()
    key.write_private_key(buf)
    return buf.getvalue()


def test_load_ssh_private_key_parses_a_real_key(rsa_private_key_pem):
    key = remote_install._load_ssh_private_key(rsa_private_key_pem)
    assert isinstance(key, paramiko.RSAKey)


def test_load_ssh_private_key_raises_on_garbage():
    with pytest.raises(remote_install.RemoteInstallError):
        remote_install._load_ssh_private_key("not a key at all")


def test_load_ssh_private_key_tolerates_a_paramiko_version_without_dsskey(monkeypatch, rsa_private_key_pem):
    """Regression test: paramiko 5.x dropped DSSKey entirely (DSA keys
    are deprecated/disabled-by-default in OpenSSH anyway) -- a real
    AttributeError against the actually-installed paramiko caught this
    the hard way during manual verification. Simulates any paramiko
    version missing an attribute this module might reference, whether
    or not the currently-pinned version happens to have DSSKey."""
    monkeypatch.delattr(paramiko, "DSSKey", raising=False)
    key = remote_install._load_ssh_private_key(rsa_private_key_pem)
    assert isinstance(key, paramiko.RSAKey)


def test_install_linux_wraps_connection_failure_in_remote_install_error(rsa_private_key_pem):
    """A real, parseable key so this actually reaches the mocked
    connect() -- a garbage key would raise for the wrong reason (key
    parsing, not connection) and never exercise this path at all."""
    with patch.object(paramiko.SSHClient, "connect", side_effect=OSError("no route to host")):
        with pytest.raises(remote_install.RemoteInstallError):
            remote_install.install_linux(
                "192.0.2.1", "ansible_svc", "http://server/bundle", ssh_private_key_pem=rsa_private_key_pem
            )


def test_install_linux_wraps_key_parse_failure_in_remote_install_error():
    with pytest.raises(remote_install.RemoteInstallError):
        remote_install.install_linux(
            "192.0.2.1", "ansible_svc", "http://server/bundle", ssh_private_key_pem="garbage"
        )


def test_install_linux_raises_when_no_credential_given():
    """Neither a private key nor a password configured -- caught before
    ever attempting a connection, with a clear message rather than
    paramiko's own (less helpful) complaint about missing auth."""
    with pytest.raises(remote_install.RemoteInstallError, match="no SSH credential"):
        remote_install.install_linux("192.0.2.1", "ansible_svc", "http://server/bundle")


def test_install_linux_runs_the_expected_remote_command(rsa_private_key_pem):
    """Doesn't touch the network: paramiko.SSHClient.connect and
    exec_command are both mocked, asserting on the command string
    actually sent rather than any real SSH behavior."""
    mock_stdout = MagicMock()
    mock_stdout.channel.recv_exit_status.return_value = 0
    mock_stdout.read.return_value = b"installed"
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""

    with patch.object(paramiko.SSHClient, "connect"), patch.object(
        paramiko.SSHClient, "exec_command", return_value=(MagicMock(), mock_stdout, mock_stderr)
    ) as mock_exec, patch.object(paramiko.SSHClient, "close"):
        result = remote_install.install_linux(
            "192.0.2.1",
            "ansible_svc",
            "http://server/bundle?install_token=abc",
            ssh_private_key_pem=rsa_private_key_pem,
        )

    assert result == {"exit_code": 0, "stdout": "installed", "stderr": ""}
    command = mock_exec.call_args[0][0]
    assert "curl" in command
    assert "http://server/bundle?install_token=abc" in command
    assert "install-linux.sh --silent" in command


def test_install_linux_falls_back_to_password_auth():
    """No private key configured, just a password -- the fallback path
    for hosts set up without a deployed key (a real, common lab setup,
    not just hypothetical)."""
    mock_stdout = MagicMock()
    mock_stdout.channel.recv_exit_status.return_value = 0
    mock_stdout.read.return_value = b"installed"
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""

    with patch.object(paramiko.SSHClient, "connect") as mock_connect, patch.object(
        paramiko.SSHClient, "exec_command", return_value=(MagicMock(), mock_stdout, mock_stderr)
    ), patch.object(paramiko.SSHClient, "close"):
        result = remote_install.install_linux(
            "192.168.158.133", "ubuntu", "http://server/bundle", ssh_password="forensics"
        )

    assert result == {"exit_code": 0, "stdout": "installed", "stderr": ""}
    assert mock_connect.call_args.kwargs["password"] == "forensics"
    assert "pkey" not in mock_connect.call_args.kwargs


def test_install_windows_wraps_connection_failure_in_remote_install_error():
    with patch("winrm.Session") as mock_session_class:
        mock_session_class.return_value.run_ps.side_effect = Exception("connection refused")
        with pytest.raises(remote_install.RemoteInstallError):
            remote_install.install_windows("192.0.2.1", "svc_provisioning", "hunter2", "http://server/bundle")


def test_install_windows_falls_back_from_https_to_http():
    """Regression test: HTTPS/5986 (the documented convention, matching
    provisioning/inventory.ini.example) was refused outright against a
    real Windows 10 target during manual verification -- plain
    `Enable-PSRemoting` only sets up an HTTP/5985 listener unless
    someone explicitly configures HTTPS. Confirms both endpoints get
    tried, in that order, and a success on the second one is returned
    rather than the first one's failure winning."""
    mock_result = MagicMock(status_code=0, std_out=b"installed", std_err=b"")
    with patch("winrm.Session") as mock_session_class:
        mock_session_class.return_value.run_ps.side_effect = [
            Exception("[WinError 10061] actively refused"),
            mock_result,
        ]

        result = remote_install.install_windows(
            "192.168.158.130", "sansdfir", "dfirrocks", "http://server/bundle"
        )

    assert result == {"exit_code": 0, "stdout": "installed", "stderr": ""}
    endpoints_tried = [call.args[0] for call in mock_session_class.call_args_list]
    assert endpoints_tried == [
        "https://192.168.158.130:5986/wsman",
        "http://192.168.158.130:5985/wsman",
    ]


def test_install_windows_runs_the_expected_powershell():
    mock_result = MagicMock(status_code=0, std_out=b"installed", std_err=b"")
    with patch("winrm.Session") as mock_session_class:
        mock_session_class.return_value.run_ps.return_value = mock_result

        result = remote_install.install_windows(
            "192.0.2.1", "svc_provisioning", "hunter2", "http://server/bundle?install_token=abc"
        )

    assert result == {"exit_code": 0, "stdout": "installed", "stderr": ""}
    ps_script = mock_session_class.return_value.run_ps.call_args[0][0]
    assert "Invoke-WebRequest" in ps_script
    assert "http://server/bundle?install_token=abc" in ps_script
    assert "cybersim-agent-setup.exe" in ps_script
    assert "/VERYSILENT" in ps_script
