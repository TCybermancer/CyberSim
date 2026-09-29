# Deploying the SMB/hang fix (agent v0.2.1) — runbook

The fix (PR #1, `fix/smb-robustness-and-action-timeout`) is **agent source**.
The agents on the puppets are **pre-built PyInstaller binaries**, so the source
change only reaches them after the agent is rebuilt and reinstalled. It does
**not** ride the "check for updates" button — that only compares GitHub release
tags and notifies; it deploys nothing.

Good news: **no Windows box is needed.** `.github/workflows/release.yml` builds
the Windows installer (windows-latest) and the Linux binary (ubuntu-latest) in
CI on a version tag, and bakes them into the server image's `install_artifacts/`.

> Do this **between runs**, never mid-run: remote install does a full silent
> reinstall per host, which interrupts that agent's traffic.

## What's already staged (this branch)

- The fix itself (agent/agent.py, agent/actions/smb_access.py) + tests.
- Version bumped to **0.2.1** in lockstep: `server/version.py`,
  `server/models.py`, `agent/models.py`, `agent/installer/cybersim-agent.iss`.

## Steps (between runs)

### 1. Merge + tag → CI builds the artifacts
```bash
# merge PR #1 into main (GitHub UI or gh)
gh pr merge 1 --repo TCybermancer/CyberSim --squash    # or via the UI
git checkout main && git pull
git tag v0.2.1 && git push origin v0.2.1
```
The tag triggers `release.yml`:
- Windows job → `cybersim-agent-setup.exe`
- Linux job → `cybersim-agent` (+ `cybersim-agent-linux.tar.gz`)
- A **GitHub Release v0.2.1** with both attached
- A published `ghcr.io/tcybermancer/cybersim-server:0.2.1` image (agent baked in)

Wait for the workflow to go green.

### 2. Refresh the running server's agent bundle
The live server (`cybersim-srv-02` @ 192.168.200.163) runs a **custom** image
(`localhost/cybersim-server:mda-medical-20260924`), not the stock ghcr image, so
swap the rebuilt binaries into the running container rather than redeploy:

```bash
# on 192.168.200.163, as ansible_svc (sudo)
cd /tmp
# fetch the v0.2.1 agent artifacts from the GitHub Release (has internet? if the
# server is airgapped, download on the deploy Mac and scp them over instead)
curl -fsSLO https://github.com/TCybermancer/CyberSim/releases/download/v0.2.1/cybersim-agent-setup.exe
curl -fsSL  https://github.com/TCybermancer/CyberSim/releases/download/v0.2.1/cybersim-agent-linux.tar.gz | tar xz  # -> cybersim-agent

# back up the current (Aug 2 / 0.2.0) artifacts, then swap in the new ones
podman exec cybersim-canary sh -c 'cp -a /app/install_artifacts /app/install_artifacts.pre-021'
podman cp cybersim-agent-setup.exe cybersim-canary:/app/install_artifacts/cybersim-agent-setup.exe
podman cp cybersim-agent            cybersim-canary:/app/install_artifacts/cybersim-agent
podman exec cybersim-canary sh -c 'chmod +x /app/install_artifacts/cybersim-agent'
```
`GET /install/agent-bundle` now serves the fixed agent immediately (no restart
needed — it streams the files off disk per request).

### 3. Reinstall the agent on the puppets
From the dashboard **/ui/install.html** (Remote install), or the `remote_install`
API, target each PAO + EMS host. Each host pulls `/install/agent-bundle` with a
one-time install token and runs the silent installer
(`cybersim-agent-setup.exe /VERYSILENT` on Windows, `install-linux.sh --silent`
on Linux), replacing the 0.2.0 agent and restarting the service.

Do the offline MDA-style hosts only if that range comes back; MDA is shut down.

### 4. Verify
- Agents re-register reporting **agent_version 0.2.1**:
  ```sql
  -- on 192.168.200.163: python3 sqlite3 /var/lib/cybersim-canary-data/cybersim.db
  SELECT host, agent_version FROM agents WHERE agent_version != '0.2.1';  -- should be empty for updated hosts
  ```
- On the next run, the SMB failure rate should drop sharply from ~70%
  (compare `completion_records` exit_status for smb_access actions before/after).

## Rollback
```bash
podman exec cybersim-canary sh -c 'rm -rf /app/install_artifacts && mv /app/install_artifacts.pre-021 /app/install_artifacts'
```
Then re-run remote install to push the previous agent back, if agents were
already updated.

## Note on the "check for updates" button
After v0.2.1 is released, `/updates/check` will show it as available and list any
agents still on 0.2.0 as outdated — but it is **notification only**. Steps 2–3
above are what actually deploys it.
