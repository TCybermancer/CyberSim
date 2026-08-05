# CyberSim Orchestrator — enterprise user-activity simulation platform (prototype)

Simulates realistic enterprise user behavior (web browsing, email,
LibreOffice document editing, SMB file-share access) for cyber-range
detection-tool validation, generating real network traffic and real
Windows/Linux host logs by driving actual applications rather than
faking log entries.

This started as a scaffold with every action module stubbed; all four
(`web_browse`, `email_send`, `office_doc`, `smb_access`) now drive real
applications end to end -- see "What's stubbed vs. what's real" below
for what's left (mTLS as an upgrade over the current bearer-token agent
auth, per-user dashboard roles, Linux SMB verification, second-source
log cross-checks). The architecture, data contracts (`models.py`),
determinism model, and provisioning flow are intended to be solid;
continue building on them in Claude Code rather than restarting.

**Documentation:** [Developer Notes](DEVELOPER_NOTES.md)

## Installation (real deployments, not the single-machine setup above)

### Server

Run the orchestrator with Docker on Linux or Windows, or install it as a
native Linux service. Pick one deployment method.

**Docker** (`server/Dockerfile`, `server/docker-compose.yml`):

The prebuilt Linux/amd64 image is published at
`ghcr.io/tcybermancer/cybersim-server:latest`. For Compose, download the
single compose file; the rest of the repository is not required.

On Linux:

```bash
mkdir cybersim-server && cd cybersim-server
curl -LO https://raw.githubusercontent.com/TCybermancer/CyberSim/main/server/docker-compose.yml
docker compose pull
docker compose up -d
```

On Windows 10/11, install Docker Desktop, enable its WSL 2 backend, and
make sure Docker Desktop is using Linux containers. Then run in PowerShell:

```powershell
New-Item -ItemType Directory -Path CyberSim-Server
Set-Location CyberSim-Server

Invoke-WebRequest `
  -Uri "https://raw.githubusercontent.com/TCybermancer/CyberSim/main/server/docker-compose.yml" `
  -OutFile "docker-compose.yml"

docker compose pull
docker compose up -d
```

Open `http://localhost:8000/ui/` and log in with the built-in `admin`
account -- password `admin` unless you pinned a different one (see
below) -- then change it right away under Settings -> Security; see
"Auth" below. Use `docker compose logs -f` to follow the server logs,
`docker compose restart` to restart it, and `docker compose down` to
stop it. The named data volume survives `down`; do not use `docker
compose down -v` unless you intend to delete CyberSim's data.

**Optional: pin the admin password up front instead of using the
default.** Worth doing for any internet-reachable deployment, so the
`admin`/`admin` default is never live even briefly. Set
`CYBERSIM_ADMIN_PASSWORD` before `docker compose up`:

```bash
export CYBERSIM_ADMIN_PASSWORD='replace-with-a-long-random-password'
```

```powershell
$env:CYBERSIM_ADMIN_PASSWORD = "replace-with-a-long-random-password"
```

It only takes effect on a startup where it's actually set -- a later
`docker compose up -d` without it won't reset a password you've since
changed via Settings -> Security.

To update later:

```bash
cd cybersim-server
docker compose pull
docker compose up -d
```

For a one-command deployment without Compose, create a named volume and use
`docker run -d --name cybersim-server --restart unless-stopped -p 8000:8000
-v cybersim-data:/data ghcr.io/tcybermancer/cybersim-server:latest` (add
`-e CYBERSIM_ADMIN_PASSWORD='replace-me'` to pin a password instead of the
default). Building from source remains available with `docker build -t
cybersim-server:local server` from a clone.

The SQLite DB lives at `/data` inside the container (`CYBERSIM_DB_PATH`,
read by `db.py`), backed by a named volume so it survives container
recreation -- verified by hand: created a run, `docker rm`'d the
container, started a fresh one from the same volume, the run was still
there. Runs as a non-root `cybersim` user inside the image (also
verified). Edit scenarios without rebuilding by bind-mounting your own
directory over `/app/scenarios` (commented example in the compose file).
Exposes on all interfaces by default -- see "OOB network separation"
above for why that needs a host firewall rule (or an explicit bind IP in
`docker-compose.yml`) restricting it to the OOB network in a real
deployment.

**Native, no Docker** (`server/install.sh` + `server/systemd/`):
```bash
cd server
sudo ./install.sh
```
Creates a dedicated `cybersim` system user, installs to
`/opt/cybersim-server` with its own venv, data at
`/var/lib/cybersim-server/cybersim.db`, and a systemd unit
(`cybersim-server.service`, `Restart=on-failure`). Verified by hand on a
real systemd host (WSL2/Kali, which ships systemd): service starts,
survives being re-run for redeploy (idempotent), stays `enabled` across
that redeploy, runs as the dedicated non-root user, API reachable and
data persists throughout. `systemctl status cybersim-server` /
`journalctl -u cybersim-server -f` to check on it.

### Agent installation (Windows or Linux)

The easiest path is the server's own install page --
`http://<server>/ui/install.html` (linked from the dashboard's header),
which has a tab for each OS. Fill in Host ID and Persona, download, run
the installer: Windows' "Orchestrator Connection" wizard page (or
Linux's install script prompts) opens **pre-filled** with this server's
own address (plus whatever Host ID/Persona you entered) -- confirm or
edit, finish, done. Both work unattended too, for scripted provisioning:
Windows' `cybersim-agent-setup.exe /VERYSILENT`, Linux's
`./install.sh --silent`, apply the same pre-filled values with no
prompts.

How the auto-link works: `GET /install/agent-bundle?host_id=...&persona=...&os=windows|linux`
(`server/app.py`, requires a logged-in dashboard session -- see "Auth"
below) zips (Windows) or tars (Linux) the pre-built installer artifacts
(`server/install_artifacts/` -- **not** checked into git, see "CI /
Releases" below for where they come from) together with a freshly
generated `install-defaults.txt` sidecar file, one line each for
server_url (taken from *that request's own* base URL -- whatever address
reached the server is what the agent should reach it at too), host_id,
persona, and that host's bearer token (minted here on first download,
reused on later downloads for the same host_id). The installer (Windows:
`agent/installer/cybersim-agent.iss`, Inno Setup; Linux:
`agent/installer/install-linux.sh`) reads that sidecar file, if present
next to it, to pre-fill its wizard page or prompts. The installer
binary/script itself is static and never regenerated per download --
only the small sidecar file is dynamic, which is what keeps this simple
and reliable rather than trying to patch a compiled binary per request.

Where the two platforms differ: Windows installs to Program Files
(admin-elevated, since registering its Scheduled Task needs that) and
autostarts via a Scheduled Task firing at user logon. Linux installs
per-user under `~/.local/share/cybersim-agent` -- **no root required**
-- and autostarts via a `systemd --user` unit, which starts at this
user's next login (covers both graphical and SSH logins on a systemd
distro via `pam_systemd`); for a headless host you want it running
without an active login session, separately run
`loginctl enable-linger "$USER"`. Both approaches exist for the same
reason: puppet hosts are meant to look like a real logged-in user
working, which is also what a real user's session actually is -- not a
background service. `./install.sh --uninstall` reverses the Linux
install.

**Remote install** -- a third tab on the same install page installs
onto a host over the network, given just its IP and OS, instead of
downloading and running an installer by hand on that host. Configure
credentials once under Settings -> Remote Install (a default Linux SSH
user + private key, and a default Windows WinRM user + password --
masked at rest same as the LLM API keys), then every remote install
after that only asks for IP/OS/Host ID/Persona.

It doesn't push installer bytes over SSH/WinRM -- the ~50MB Windows
installer alone makes that a bad idea over WinRM's SOAP transport.
Instead `POST /install/remote` (`server/app.py`, admin only) mints a
short-lived, single-use `install_token` (`db.create_install_token`),
logs into the target with the stored credentials
(`server/remote_install.py` -- `paramiko` for SSH, `pywinrm` for WinRM),
and runs a one-line remote command that has the *target* pull its own
bundle from `GET /install/agent-bundle?install_token=...` -- the exact
same endpoint the manual download page uses, just authenticated
differently: a valid `install_token` (consumed on first use, rejected
after 10 minutes) stands in for the usual dashboard session, so the
target host never needs a standing credential of its own. The token
pins the host_id/persona/os it was minted for, ignoring anything
different in the request's own query string, so it can't be redirected
to install as a different host than an admin actually approved.

### Auth

Three independent layers (see `server/auth.py`'s module docstring for
the reasoning behind the first two):

- **Dashboard <-> browser**: per-user accounts with a role of `admin` or
  `viewer` (session cookie). Viewers can see everything (topology, runs,
  ledger, scoring) but can't mutate range state -- launching runs,
  writing scenarios/schedules, and downloading install bundles (which
  mints a live agent credential) all require `admin`. A built-in
  `admin` account is bootstrapped at first startup: set
  `CYBERSIM_ADMIN_PASSWORD` to pin its password, or leave it unset and
  the server defaults it to the password `admin` once (a known,
  out-of-the-box credential rather than a generated one -- a loud
  warning prints at startup either way, and it's never re-applied on a
  later startup once the account exists, so it won't stomp on a
  password you've since changed). Change it immediately after first
  login under Settings -> Security (`POST /auth/change-password`,
  self-service for whichever account you're logged in as, any role --
  not just admin). Once logged in as an admin, create more accounts
  (including more admins) at `http://<server>/ui/users.html` or via
  `POST /users`. Log in at `http://<server>/ui/login.html`; the
  dashboard redirects there automatically on any 401.
- **Agent <-> server**: each host gets its own bearer token, minted the
  first time `/install/agent-bundle` is downloaded for that host_id and
  required on every register/poll/ledger call from that host afterward.
  This is a smaller, faster-to-ship step than the mTLS this project's
  own TODOs originally called for -- see "Still stubbed" above for that
  tradeoff.
- **Remote-install target <-> server**: a short-lived, single-use
  `install_token` (10-minute expiry, see `db.INSTALL_TOKEN_TTL_SECONDS`),
  minted by `POST /install/remote` and consumed by the one
  `GET /install/agent-bundle` call it authorizes -- see "Agent
  installation" above. Exists so a remote-install target never needs a
  standing credential of its own; the admin's actual SSH/WinRM
  credentials (Settings -> Remote Install) never leave the server.

### Live content generation & organization scenarios

Scenarios can carry two kinds of extra metadata beyond `persona`:

- **`org` / `department`** (top-level, e.g. `org: Metro Airport`,
  `department: Executive`) -- purely descriptive grouping metadata for
  the dashboard; `scenario_engine.py` never reads them, so a scenario
  without them behaves exactly as before.
- **`content_brief`** (per `email_send` step, inside that step's
  `params:`) -- a short instruction ("push back on the Q3 budget,
  citing insufficient funds") that, when the server is in "connected"
  mode (see `http://<server>/ui/settings.html`), gets sent to an LLM
  (Anthropic / OpenAI / a local OpenAI-compatible endpoint -- your
  choice, admin only) to write the actual subject/body live at
  run-launch time. A step without `content_brief` is untouched; a step
  *with* one still needs a `template:` set too, since that's the
  fallback used whenever live generation doesn't apply or fails:
  - An explicit `seed` was passed (replay mode) -- resolve()'s
    byte-identical-given-the-same-seed guarantee always wins; live
    generation only ever runs for a genuinely fresh, unseeded launch.
  - `network_mode` is "airgapped" (the default) -- no outbound call is
    even attempted.
  - The LLM call fails, times out, or returns something that doesn't
    parse as `Subject: ...\n\n<body>` -- logged, then the launch
    proceeds with the static template rather than failing outright.

  Deliberately scoped to `email_send` only: unlike free-form email
  prose, `web_browse` targets and `smb_access` paths have to correspond
  to things that actually exist in the range (a real reachable URL, a
  real file on a real share) -- an LLM can't safely invent those at run
  time. Realism for those two comes from writing good, role-appropriate
  static content into the scenario directly, not live generation.

  See `server/content_gen.py` for the provider abstraction and
  `server/app.py`'s `_apply_live_content` for exactly where this plugs
  into `POST /runs` (and the recurring-schedule scheduler loop, which
  shares the same `_launch_run` core).

The installer requires admin (creates a Scheduled Task, not a Windows
service -- a puppet host is meant to look like a real logged-in user
working, which is also what a real user's session actually *is*, so the
agent runs via a `/sc onlogon` task under that user rather than as a
background SYSTEM service). Two real bugs surfaced building and testing
this, worth knowing if you touch `cybersim-agent.iss`:
- Inno Setup's `""`-doubling string-escape convention and `schtasks
  /tr`'s own internal `\"..\"` quoting for a quoted path *with*
  trailing arguments don't compose safely by hand -- a nested-quoting
  attempt silently produced a malformed `/tr` value (schtasks exit code
  1, task never created, no visible error anywhere in Inno's log beyond
  the bare exit code). Sidestepped entirely rather than fixed: the
  Scheduled Task's command has no arguments at all, since `agent.py`
  already resolves `config.yaml` next to its own executable by default
  when frozen (`_default_config_path()`) -- no `--config` argument, no
  embedded quoting, no problem.
- A `/sc onlogon` trigger specifically (not scheduled-task creation in
  general -- `/sc once` worked fine) got "Access is denied" for a
  **non-admin** task creation on the Windows machine this was tested on,
  even though nothing else about the setup was restricted. Whether
  that's this specific machine's policy or a general Windows constraint
  on that trigger type wasn't fully isolated -- but `PrivilegesRequired=
  admin` in the `.iss` sidesteps it either way, and matches the existing
  Ansible provisioning's own elevated pattern (`become: true` / admin
  WinRM) for puppet host setup regardless.

**Rebuilding the agent installer by hand** (after any agent code change,
if you're not relying on CI -- see "CI / Releases" below for the
automated version). PyInstaller can't cross-compile, so build each
platform's artifact on that platform.

Windows:
```bash
cd agent
pip install pyinstaller
pyinstaller cybersim-agent.spec        # -> dist/cybersim-agent.exe
# playwright's PyInstaller hook (ships with the playwright package
# itself) is picked up automatically; no extra config needed

cd installer
iscc cybersim-agent.iss                # needs Inno Setup 6 installed
# -> installer/output/cybersim-agent-setup.exe

# then make the server's download page serve the new build:
cp installer/output/cybersim-agent-setup.exe ../../server/install_artifacts/
```

Linux:
```bash
cd agent
pip install pyinstaller
pyinstaller cybersim-agent.spec        # -> dist/cybersim-agent (no installer to
                                        # build here -- install-linux.sh is
                                        # plain checked-in source, no compile step)

# then make the server's download page serve the new build:
cp dist/cybersim-agent installer/install-linux.sh ../server/install_artifacts/
chmod +x ../server/install_artifacts/cybersim-agent ../server/install_artifacts/install-linux.sh
```

### Testing

```bash
# server + scoring (pure Python, no browser/Office/SMB dependency)
pip install -r server/requirements.txt -r scoring/requirements.txt pytest httpx
pytest server/tests scoring/tests -v

# agent (OS-portable logic only -- see below for what's excluded)
pip install -r agent/requirements.txt pytest
pytest agent/tests -v
```
Run from the repo root; each of `server/`, `scoring/`, `agent/` has its
own `conftest.py` doing the sys.path setup needed for that component's
own import convention (flat for server/agent, package-relative for
scoring -- see each conftest.py for why). `server/tests` uses an
isolated throwaway SQLite DB per test (never touches a real
`cybersim.db`) and FastAPI's `TestClient` against the real API, so it
covers real bugs found this way earlier: the dispatch-timing fix, the
`os`/`persona`-clobbering bug, the concurrent-run guard, the
install-bundle input validation, and (in `remote_install.py`,
non-mocked) `paramiko` dropping `DSSKey` in its 5.x line -- a real
`AttributeError` on the actually-installed version that a fully-mocked
test would never have caught.

**What's covered vs. not**: `scenario_engine`, `db`, `app`'s API surface,
and `scoring`'s matching/scoring logic are fully unit-tested. The agent
action modules are unit-tested only for their OS-portable pieces --
template rendering (`email_send`, `smtplib` mocked), local file-copy
helpers (`smb_access`, real temp-dir I/O), resource-path resolution
(`_bundle`), and the `PLAYWRIGHT_BROWSERS_PATH` fix (`web_browse`).
Actually driving a real browser, real LibreOffice, or a real SMB share
is *not* covered by this suite -- those were verified by hand (see each
module's entry above) and would need a much heavier CI setup (a real
LibreOffice install, a downloaded Chromium, a real or loopback SMB
share) to automate. Worth doing eventually; out of scope for now.
Similarly, `remote_install.py`'s actual SSH/WinRM connection and command
execution are mocked in `test_remote_install.py` (real key parsing and
error-wrapping are covered; a real network round-trip against an actual
target host is not) -- verified by hand instead, against an
intentionally-unreachable test IP, confirming both the connection-
failure path and the real `paramiko` key-parsing path work end to end
through the live API.

### CI / Releases

`.github/workflows/test.yml` runs the suite above on every push/PR to
`main` (`ubuntu-latest` -- fast, and nothing in the covered scope needs
Windows).

`.github/workflows/release.yml` runs after pushes to `main`, version tags,
or manual dispatch, with three jobs. Its Windows job builds the agent exe
and installer exactly like the by-hand steps above, deriving the
installer's version from the tag (`cybersim-agent.iss`'s `#define
MyAppVersion` is `#ifndef`-guarded so `iscc /DMyAppVersion=X.Y.Z` can
override it without editing the file -- verified locally that the
version string actually lands in the compiled binary, not just that it
compiles). A separate Linux job builds the agent binary the same way and
packages it with `install-linux.sh` into a standalone tarball. Both jobs
upload their build as a workflow artifact always, and additionally
attach it to a GitHub Release when triggered by a real tag. A third job,
depending on both, embeds both platforms' artifacts in the server image
and publishes `ghcr.io/tcybermancer/cybersim-server` with `latest`,
commit-SHA, and (for releases) semantic-version tags.

Source deployments can grab the latest release's artifacts and place
them at `server/install_artifacts/cybersim-agent-setup.exe` (Windows) and
`server/install_artifacts/cybersim-agent` +
`server/install_artifacts/install-linux.sh` (Linux, both `chmod +x`'d --
or build them yourself per above) before `/install/agent-bundle` has
anything to serve for that platform -- all three are `.gitignore`d on
purpose: a checked-in binary grows the repo forever since git can't
meaningfully diff it (already tripped GitHub's 50MB size warning twice
before this became CI-driven), and the shell script, while small enough
to check in, is only ever a copy of the real source at
`agent/installer/install-linux.sh` -- keeping both in git would just
invite them drifting apart.
