#!/usr/bin/env python3
"""Run a command inside a VM's guest OS via VMware Tools guest operations,
with no network path to the guest required (uses the vSphere/ESXi API's
guest RPC channel instead). Fills a real gap: community.vmware ships no
"run a program in the guest" module -- only file copy/fetch/mkdir.

Used by roles/dc_template_prep to bootstrap WinRM on a VM before Ansible
has any network route to it yet.

Credentials come from the environment, never argv, so they never show up
in `ps`, shell history, or Ansible's command-line audit log:
  ESXI_HOST, ESXI_USER, ESXI_PASSWORD  -- vCenter/ESXi API endpoint
  GUEST_USER, GUEST_PASSWORD           -- Windows local account inside the VM
"""
import argparse
import os
import ssl
import sys
import time
import urllib.request

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim


def find_vm(content, name):
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        for v in container.view:
            if v.name == name:
                return v
    finally:
        container.Destroy()
    return None


def run(si, vm, creds, command, timeout, no_wait=False):
    content = si.RetrieveContent()
    pm = content.guestOperationsManager.processManager
    fm = content.guestOperationsManager.fileManager

    out_path = "C:\\Windows\\Temp\\ansible_guestexec_{}.txt".format(int(time.time() * 1000))
    spec = vim.vm.guest.ProcessManager.ProgramSpec(
        programPath="C:\\Windows\\System32\\cmd.exe",
        arguments='/c {} > "{}" 2>&1'.format(command, out_path),
    )
    pid = pm.StartProgramInGuest(vm, creds, spec)

    if no_wait:
        # For commands that shut down or reboot the guest mid-run (sysprep
        # /shutdown, restart) -- polling ListProcessesInGuest afterward
        # would just fail once the guest-tools RPC channel drops.
        return None, "<fire-and-forget: pid {} started, not awaited>".format(pid)

    exit_code = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        procs = pm.ListProcessesInGuest(vm, creds, [pid])
        if procs and procs[0].endTime:
            exit_code = procs[0].exitCode
            break
        time.sleep(1)
    else:
        raise TimeoutError("command did not finish within {}s".format(timeout))

    output = ""
    try:
        finfo = fm.InitiateFileTransferFromGuest(vm, creds, out_path)
        url = finfo.url.replace("*", os.environ["ESXI_HOST"])
        rctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        rctx.check_hostname = False
        rctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=rctx, timeout=15) as resp:
            output = resp.read().decode(errors="replace")
    except Exception as exc:  # output file may not exist if the command itself failed to start
        output = "<no output: {}>".format(exc)

    return exit_code, output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--command", required=True, help="Command to run via cmd.exe /c")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--no-wait", action="store_true", help="Fire the command and return immediately (for sysprep/shutdown/restart)")
    args = parser.parse_args()

    esxi_host = os.environ["ESXI_HOST"]
    esxi_user = os.environ["ESXI_USER"]
    esxi_password = os.environ["ESXI_PASSWORD"]
    guest_user = os.environ["GUEST_USER"]
    guest_password = os.environ.get("GUEST_PASSWORD", "")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    si = SmartConnect(host=esxi_host, user=esxi_user, pwd=esxi_password, sslContext=ctx)
    try:
        content = si.RetrieveContent()
        vm = find_vm(content, args.vm_name)
        if vm is None:
            print("VM not found: {}".format(args.vm_name), file=sys.stderr)
            return 2

        creds = vim.vm.guest.NamePasswordAuthentication(username=guest_user, password=guest_password)
        exit_code, output = run(si, vm, creds, args.command, args.timeout, no_wait=args.no_wait)
        sys.stdout.write(output)
        if args.no_wait:
            return 0
        return exit_code if exit_code is not None else 1
    finally:
        Disconnect(si)


if __name__ == "__main__":
    sys.exit(main())
