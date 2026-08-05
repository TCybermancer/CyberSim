# Developer Notes

## Authorized use only

This drives real applications to produce genuine network traffic and
host activity that's deliberately built to *look like* a real user --
including, via a scenario's `should_alert: true` steps, activity meant
to resemble malicious behavior. That makes it dual-use: exactly what
makes it useful for validating detection tooling is what would make it
harmful pointed at something you don't control.

Use this only in an environment you own or are explicitly authorized to
test in -- a cyber range, an isolated lab, or a sanctioned engagement
with written authorization. Never point it at production systems,
third-party networks, or anything you don't have clear permission to
test. The OOB/in-band network separation described below exists
specifically to keep this contained to the range it's deployed in --
don't defeat that isolation, and don't reuse real credentials (domain
accounts, API keys) in scenario configs for a system this touches.

## Architecture

```
                     OOB (management) network — 10.99.0.0/24
        ┌─────────────────────────────────────────────────────┐
        │                                                       │
   ┌────┴────┐   poll / ledger / provisioning (SSH/WinRM)  ┌────┴─────┐
   │ server  │◄────────────────────────────────────────────┤  agent   │
   │(FastAPI)│                                              │ (host)  │
   └────┬────┘                                              └────┬────┘
        │                                                        │
        │ scoring harness reads ledger                    NIC2 (OOB)
        ▼                                                        │
   ┌─────────┐                                              ╔════╧════╗
   │ scoring │                                              ║  host   ║
   └─────────┘                                              ╚════╤════╝
                                                                  │
                                                             NIC1 (in-band)
                                                                  │
        ┌─────────────────────────────────────────────────────┐│
        │         In-band range network (monitored)            ◄┘
        │  puppet mail server · file server · web targets · DC │
        └─────────────────────────────────────────────────────┘
```

- **Server** (`server/`): FastAPI orchestrator. Resolves scenario YAML
  into concrete, timestamped `ActionSpec`s via a seeded PRNG
  (`scenario_engine.py`), stores them as ground truth, and serves them to
  agents on poll. Also collects the ledger (intent + completion records)
  that the scoring harness consumes.
- **Agent** (`agent/`): runs on each simulated-user host. Polls the
  server over its OOB NIC, executes actions by driving real applications,
  and reports ground truth back over OOB. `actions/` is a pluggable
  registry — one module per action type.
- **Provisioning** (`provisioning/`): Ansible playbooks for adding
  puppet users/services on Linux (SSH) and Windows (WinRM), plus an
  optional AD account on the domain controller. All provisioning traffic
  rides OOB.
- **Scoring** (`scoring/`): standalone module + CLI. Reads a run's
  ground-truth ledger over the API (it doesn't touch the SQLite DB
  directly — same read path the UI uses) and a detection tool's alert
  export, matches them by host + time window, and reports precision/
  recall/detection-latency overall and per action_type/persona. See
  "Scoring harness" below.
- **Front-end** (`server/static/`): a static, dependency-free HTML/JS
  page FastAPI serves itself at `/ui/` (no build step, no CDN calls —
  this may run in an air-gapped range), including `/ui/install.html` for
  downloading the agent installer, Windows or Linux (see "Installation"
  below). Browse scenarios, pick registered hosts, launch a run, and watch its
  ledger fill in live. It's a thin client over the read-only GET
  endpoints below; it doesn't add any write
  capability the API didn't already have.

## Out-of-band (OOB) network separation

Every simulated-user host is dual-homed:

- **NIC1 (in-band)**: same network/VLAN/DNS the detection tooling under
  test actually monitors. All puppet activity (browsing, email, SMB)
  egresses here.
- **NIC2 (OOB)**: separate, non-routed management network. Carries agent
  polling, ledger reporting, and Ansible provisioning traffic only.

The OOB network must have **no route** to/from the in-band network — not
just a firewall rule. `agent.py`'s `bound_session()` source-binds
outbound sockets to the OOB IP as a belt-and-suspenders measure, but the
real guarantee should come from the network topology itself (separate
virtual switches / no routing table entry between the two).

**Validation step worth automating early**: run a full scenario, packet
capture the in-band segment for the entire duration, and assert zero
packets reference the OOB subnet, the agent binary's traffic pattern, or
the orchestrator's hostname/IP. That capture-and-assert check is a good
candidate for its own CI-style test in this repo.

## Provisioning a lab domain controller (`provisioning/`)

`build_domain_controller.yml` clones a lab Active Directory domain
controller from a prepared VMware template, promotes it as the first DC
of a brand-new forest, and finishes DNS (AD-integrated forward zone,
reverse lookup zone, external forwarder) so client machines on the
in-band network are actually ready to join -- not just installed.
Dual-homed like every other range host: an OOB NIC (DHCP) carries all
Ansible/WinRM traffic below, the in-band NIC is what the domain actually
serves.

**One-time template prep** (`prepare_dc_template.yml` +
`roles/dc_template_prep/`): generalizes an existing, already-installed
Windows Server VM into a reusable template via sysprep. Runs entirely
over VMware guest operations (`scripts/vmware_guestexec.py` --
community.vmware ships modules for guest file copy/fetch, but none that
run an arbitrary program in the guest, so this fills that gap directly
against the vSphere/ESXi API) rather than WinRM, since the source VM may
not even be on a network the control node can reach yet.

There's exactly one step in that role that cannot be automated
headlessly, by Windows' own design rather than a limitation of this
playbook: any *non-console* admin action from a local account other
than the built-in RID-500 `Administrator` gets a UAC-filtered token (this
is the same restriction that makes plain WinRM/PsExec-style remote admin
fail for a custom local admin account until
`LocalAccountTokenFilterPolicy` is set) -- and setting that registry key
is itself an admin action, so it can't bootstrap itself. Log into the
source VM's console once (the ESXi web UI is fine), elevate, and run
`roles/dc_template_prep/files/bootstrap_console.ps1`. Everything after
that -- unjoining any existing domain, copying the sysprep answer file,
generalizing, converting to a template, and every subsequent clone -- is
fully scripted.

**Per-deployment** (`build_domain_controller.yml` +
`roles/dc_provision/`, `roles/dc_ad_forest/`, `roles/dc_dns_config/`):
prompts for the domain name, DC hostname, in-band network CIDR (the
static IP defaults to that network's `.2`), which port group is in-band
vs. OOB, the DNS forwarder, and the DSRM password; clones the template
with a dual-NIC VMware guest customization spec; registers the new DC as
an in-memory Ansible host once its OOB NIC gets a DHCP lease;
`microsoft.ad.domain` installs AD DS + DNS and promotes the forest; then
DNS gets finished off (forwarder, reverse zone, scavenging, firewall
rule groups, NIC network-category fix so Windows Firewall's Public
profile doesn't block AD/DNS traffic from clients).

Standalone-ESXi-specific quirks worth knowing if you touch these roles:
`community.vmware.vmware_guest` has no vCenter to infer a datacenter
from, so `folder` must be given explicitly as `/ha-datacenter/vm` --
that's always the datacenter name a standalone ESXi host reports, not a
per-environment value. There's also no REST "convert VM to template"
equivalent without vCenter, so `dc_template_prep` resolves the VM's
numeric vmid via `vim-cmd vmsvc/getallvms` and calls `vim-cmd
vmsvc/markastemplate <vmid>` directly over SSH to the host (see the
`[esxi]` group in `inventory.ini.example`) instead.

See `provisioning/group_vars/all/vars.yml.example` and
`vault.yml.example` for what to copy, fill in, and (for the vault file)
encrypt with `ansible-vault encrypt group_vars/all/vault.yml` before
running either playbook.

## Determinism for validation

The scoring harness needs an independent ground-truth source to check
detection-tool output against. The ledger is that source:

1. **`ActionSpec`** — what the scenario engine *intended* to happen,
   fully resolved (no randomness left) at run-creation time. Stored
   immediately.
2. **`IntentRecord`** — logged by the agent immediately *before*
   executing, over OOB. Survives agent crashes mid-action.
3. **`CompletionRecord`** — logged immediately *after*, with
   `observed_side_effects` — this is where each action module should
   report something independently checkable (a Message-ID, a file hash,
   an HTTP status), not just "done".

Two modes, both supported by `scenario_engine.resolve()`:

- **Deterministic** — pass an explicit `seed` to `/runs`. Identical
  scenario + seed + start_time always produces byte-identical
  `ActionSpec`s. Use this for regression-testing a specific detection
  rule change (before/after must see the exact same input).
- **Distributional** — omit the seed; one is generated and returned in
  the response so the run is still fully reconstructable later. Use this
  for broader validation, so your "normal user noise" isn't so
  repeatable that ML-based detectors overfit to your simulation's tells.

`GET /runs/{run_id}/ledger` returns everything joined by `action_id` —
this is the input the scoring harness needs.

## Scoring harness

```bash
cd scoring
pip install -r requirements.txt
cd ..   # run from CyberSim/, the parent of scoring/, so `scoring` is importable

python -m scoring.cli --run-id <run_id> --alerts alerts.json
python -m scoring.cli --run-id <run_id> --alerts alerts.csv --format json > report.json
```

Fetches `GET /runs/{run_id}/ledger` from the orchestrator (same read path
the UI uses — the harness never touches the SQLite DB directly) and a
detection tool's alert export (`scoring/alerts.py` documents the expected
JSON/CSV shape — reshape whatever your tool exports into it), matches
them by host + a time window around each action's real execution
(`scoring/matcher.py`), and reports precision/recall/detection latency —
overall, and broken out per `action_type` and per `persona`
(`scoring/report.py`). `should_alert=true` actions (injected red-team
steps) are the positive class; everything else is the negative class an
alert on which counts as a false positive. Alerts matching no action at
all count as false positives at the run level too, since during a run
all in-band traffic is puppet-generated — the ground truth should be
able to explain every alert.

Matching by host + time window depends on actions actually being spread
out in real execution time the way their `intended_start` implies --
`db.pending_actions_for_host()` now enforces that (see below).

## What's stubbed vs. what's real

**Real / functional as-is:**
- FastAPI server, SQLite ledger, poll/dispatch loop
- Seeded scenario resolution (deterministic and distributional)
- Agent registration/poll/report loop, OOB source-binding
- Ansible playbook structure for Linux/Windows/AD provisioning
- `agent/actions/email_send.py` — real `smtplib` delivery, with subject
  and body rendered from `agent/templates/<name>.txt` (`string.Template`,
  `$var` syntax). SMTP endpoint/credentials come from the `smtp:` block in
  `config.yaml` (kept out of `ActionSpec.params` since it's a deployment
  setting, not scenario content). Returns a real RFC-2822 `Message-ID`
  and the server's per-recipient refusal dict as `observed_side_effects`
  — the Message-ID is what to cross-check against the mail server's own
  delivery log for second-source confirmation. Local testing: point
  `smtp.host`/`smtp.port` at a [MailHog](https://github.com/mailhog/MailHog)
  instance (SMTP on `:1025`, inspect sent mail at `http://localhost:8025`)
  or `python -m smtpd -c DebuggingServer -n 127.0.0.1:1025` (stdlib,
  removed in 3.12; bind the IPv4 address explicitly -- `-n localhost:1025`
  binds `[::1]` only on some setups and silently refuses IPv4 clients) to
  print received messages to stdout.
- `agent/actions/web_browse.py` — real Playwright (sync API) driving
  Chromium via a persistent profile directory keyed by `config['persona']`
  (`browser:` block in `config.yaml`), so cookies/history genuinely
  accumulate across actions and runs like a real user's browser. Navigates
  to `params['target']`, then does a best-effort scroll/click traversal
  for `duration_seconds` (a dead link or off-page click doesn't fail the
  action — only the initial navigation does). Returns final URL, real
  HTTP status, page title. Verified locally against a throwaway local
  HTTP server: real navigation, real click-through to a second page
  during the browse window, and a genuine Chromium `History` sqlite file
  with both visited URLs recorded in the profile directory afterward.
  Needs `playwright install chromium` after `pip install -r
  requirements.txt` (one-time, downloads the browser binary to a
  user-level cache shared across venvs). Ship with `headless: false`
  (default) — some detection tooling fingerprints headless Chromium, and
  a real user's browser isn't headless anyway; `headless: true` is fine
  for CI/dev but uses a lighter "headless shell" build with reduced
  profile persistence, which is why local testing here used `headless:
  false` to get a real `History` file.
- `agent/actions/office_doc.py` — real LibreOffice UNO automation via
  `agent/actions/_uno_worker.py`. LibreOffice's Python UNO bridge
  (`pyuno.pyd`/`libpyuno.so`) is a compiled extension built against
  LibreOffice's own bundled Python (confirmed by hand: it will not
  import under a regular venv — different build, different ABI), so
  `office_doc.py` runs the real automation as a subprocess under
  LibreOffice's *own* `python[.exe]` (`office:` block in `config.yaml`
  points at both `soffice_path` and, optionally, `bundled_python_path`),
  using LibreOffice's own shipped `officehelper.bootstrap()` to launch a
  headless instance and connect. Opens the target document (creating a
  fresh one from the app's factory if it doesn't exist yet), edits a
  cell/inserts text, holds it open+dirty for `duration_seconds` like a
  real user working on it, saves, closes, and cleanly terminates the
  office process. Returns real SHA-256 hashes before/after. One gotcha
  hit while building this: `officehelper.bootstrap()` only quotes its
  *own* auto-detected `soffice` path for `shell=True`, not a
  caller-supplied one — a path with a space (`C:\Program Files\...`)
  needs quoting yourself before passing it in, or the space splits the
  command. Verified locally end-to-end (installed LibreOffice via
  `winget` for this): real `soffice.bin` process launch, real `.xlsx`
  file created and then re-edited with a changed hash on a second run,
  clean process shutdown (no orphaned `soffice.bin`), and a clean
  reported failure for an unsupported `app` value.
- `agent/actions/smb_access.py` — real file I/O against a UNC path
  (Windows: direct, no drive-letter mapping needed for read access;
  `net use` first if `smb.username`/`password` are set, torn down after)
  or a `mount.cifs` mount (Linux — written against the documented
  interface but *not* exercised on the Windows sandbox this was built
  on, since there's no way to test that branch here regardless of
  environment). Verified locally against Windows' pre-existing `C$`
  admin share over loopback (`\\localhost\C$\...` — deliberately did not
  create a new share for testing, since that's a system-settings change;
  the built-in admin share exercises the same real SMB redirector without
  touching config) — real directory listing, real file copy with a
  correct hash, and a clean reported failure when a requested filename
  doesn't exist on the share.
- `server/static/` — the front-end described above.
- `scoring/` — the scoring harness described above.
- Installation: `server/Dockerfile` + `docker-compose.yml`, `server/
  install.sh` + `systemd/` (native, no Docker), `agent/cybersim-agent.spec`
  + `agent/installer/cybersim-agent.iss` (Windows installer, PyInstaller +
  Inno Setup) + `agent/installer/install-linux.sh` (Linux installer, same
  PyInstaller binary + a plain shell script instead of Inno Setup — per-
  user install, `systemd --user` unit instead of a Scheduled Task) and
  `GET /install/agent-bundle` (the auto-linking download page at
  `/ui/install.html`, now with a tab per OS). See "Installation" below —
  the server/Docker/native-service paths, the Windows agent installer,
  and the Linux installer are all verified by hand end to end now,
  including install-linux.sh's config.yaml/YAML-escaping/`--uninstall`
  logic against a real systemd host (initially only exercised with a
  mocked `systemctl`, since no real systemd host was available while
  building it -- see the Remote Install bullet below for how that gap
  got closed).
- Remote install (`server/remote_install.py`, `POST /install/remote`,
  `GET /install/agent-bundle`'s `install_token` param, the dashboard's
  Install agent → Remote Install tab): given a target's IP and OS,
  logs in over SSH (`paramiko`, Linux) or WinRM (`pywinrm`, Windows)
  using credentials configured once under Settings → Remote Install,
  and runs a short remote command that has the *target* pull its own
  bundle from this server rather than pushing installer bytes over
  SSH/WinRM (the Windows installer alone is ~50MB -- bad fit for
  WinRM's SOAP transport). See "Installation" below and that module's
  docstring. Key-parsing, password-auth fallback, WinRM's HTTPS-then-
  HTTP fallback, and error-wrapping are all unit-tested
  (`server/tests/test_remote_install.py`); a real SSH/WinRM round-trip
  against an actual target isn't, by nature of what a unit test can
  reach -- verified by hand instead against two real, separate machines
  (a SIFT Workstation and a Windows 10 box on a private VMware network),
  with a full install actually completing and confirmed on each (real
  systemd unit `active (running)`; real Scheduled Task registered "At
  logon time"), then uninstalled again. See "Testing" below for the
  three real bugs that surfaced only by testing against real, separate
  targets -- none of them were reachable by mocking the SSH/WinRM
  layer, however thoroughly.
- Mail server (Settings -> General's "Mail server" fields,
  `server/app.py`'s `_apply_mail_server_override`,
  `agent/actions/email_send.py`'s `params.smtp_host`/`smtp_port`
  precedence over local `config.yaml`): the Option A outcome of the
  mail-architecture discussion -- one shared SMTP relay for every org,
  not real per-org mail servers relaying to each other (Option B,
  tabled). Configured once, injected into every `email_send`
  `ActionSpec`'s params at run-launch time -- seeded replays included,
  unlike live content generation, since which relay handles mail isn't
  a content-determinism concern -- so changing it takes effect on the
  next launched run with no agent-side change. See "Mail server" below.
- Automated tests (`server/tests/`, `scoring/tests/`, `agent/tests/`,
  70 tests) and CI (`.github/workflows/`) — see "Testing" and "CI /
  Releases" below for scope (full coverage for `server`/`scoring`; agent
  action modules covered for OS-portable logic only, real
  browser/Office/SMB driving stays hand-verified).

**Twelve bugs fixed along the way, worth knowing about if you're touching
these files:**
- `ActionSpec`/`IntentRecord`/`CompletionRecord` all carry `datetime`
  fields. Calling Pydantic's (deprecated) `.dict()` on them before
  `json.dumps`-ing left those fields as real `datetime` objects, which
  the stdlib JSON encoder can't serialize -- this broke *every* ledger
  write (`POST /runs`, `POST /ledger/intent`, `POST /ledger/completion`)
  the first time a real action module (`email_send`) actually ran end to
  end. Fixed by switching every `.dict()` call (in `agent.py` and
  `app.py`) to `.model_dump(mode="json")`, which serializes datetimes to
  ISO strings first. If you add a new record type with a datetime field,
  use `model_dump(mode="json")` for it too.
- `GET /agents/{host}/poll` called `db.upsert_agent(host, "unknown",
  None, ...)` on every poll, overwriting the real `os`/`persona` that
  `POST /agents/register` had set moments earlier -- since agents poll
  far more often than they register, the `agents` table was permanently
  stuck showing `unknown`/blank. Fixed with `db.touch_agent()`, which
  only updates `last_seen`.
- `db.pending_actions_for_host()` handed the agent *every* pending action
  on its very first poll with no regard for `ActionSpec.intended_start`,
  and `agent.py` executed whatever it got immediately, back to back. A
  whole scenario's actions ended up landing within a few seconds of each
  other in real time instead of spread across the minutes the schedule
  intended -- which both made alert-to-action time-window matching in
  the scoring harness ambiguous, and (more importantly) doesn't look
  like realistic user behavior to whatever's watching. Fixed:
  `pending_actions_for_host()` now takes `now` and only returns (and
  marks dispatched) actions whose `intended_start` has actually arrived;
  actions scheduled further out stay pending and get handed out on a
  later poll. If the agent crashes mid-run, not-yet-ready actions are
  simply still pending afterward -- no separate durability concern, since
  "dispatched" already meant "handed to an agent" before this fix too.
- `cybersim-agent.iss`'s Scheduled Task command originally tried to nest
  schtasks' own `/tr "\"path\" args"` quoting inside Inno Setup's `""`
  string-escaping by hand -- silently produced a malformed `/tr` value
  (schtasks exit code 1, task never created, nothing informative in
  Inno's install log beyond that exit code). Fixed by dropping the
  `--config` argument from the task's command entirely, since
  `agent.py` already resolves `config.yaml` next to its own executable
  by default when frozen -- no arguments, no nesting, no problem. See
  "Installation" below.
- Relatedly: a `/sc onlogon` Scheduled Task trigger specifically got
  "Access is denied" when created without admin rights on the machine
  this was tested on (other trigger types worked fine unprivileged).
  Fixed by requiring admin for the installer (`PrivilegesRequired=admin`
  in `cybersim-agent.iss`), which also matches the existing Ansible
  provisioning's own elevated pattern for puppet host setup.
- `GET /install/agent-bundle`'s `host_id`/`persona` query params flowed
  unsanitized into `install-defaults.txt`, then got string-concatenated
  directly into `config.yaml` by the installer's Pascal script (`'host_id:
  "' + value + '"'`). A value containing a `"` and a newline could break
  out of the quoted YAML string and inject arbitrary config -- on an
  endpoint that's still unauthenticated (see "Still stubbed" below), so
  anyone who could get a target to click a crafted download link
  controlled what landed in that target's `config.yaml`. Fixed in two
  layers: `app.py` now restricts both params to a safe charset
  (`^[A-Za-z0-9._-]{0,64}$`, rejecting anything else with a 422) so bad
  input never reaches the sidecar file at all, and `cybersim-agent.iss`
  now escapes quotes/backslashes before embedding any value in YAML
  (`YamlEscape`) as defense in depth, in case that file is ever
  hand-edited instead of generated by the server.
- Nothing checked for clock drift between a host and the server, even
  though the scoring harness's alert-to-action matching (`scoring/
  matcher.py`) is a time-window heuristic that quietly degrades if they
  disagree -- there was no error, just increasingly wrong matches. Fixed:
  `POST /agents/register` now accepts an optional `client_time` (agent.py
  sends its own clock on every registration) and returns
  `clock_drift_seconds`; the agent prints a loud warning if drift exceeds
  5 seconds.
- Nothing stopped two runs from targeting the same host at once --
  their actions would interleave in real execution time with no record
  of which run an alert should be attributed to, corrupting scoring for
  both. Fixed: `POST /runs` now checks `db.active_runs_for_hosts()`
  (any host with an action_spec that has no completion_record yet,
  dispatched or not) and returns 409 listing which hosts are still mid-run
  instead of silently double-booking them.
- `remote_install.py`'s SSH key loader hardcoded
  `[paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey,
  paramiko.DSSKey]` -- paramiko 5.x dropped `DSSKey` entirely (DSA keys
  are deprecated/disabled-by-default in OpenSSH anyway), so with that
  version actually installed, *every* remote install attempt raised a
  raw `AttributeError` before ever reaching the connection attempt --
  caught manually in the browser, not by the test suite, since
  `test_app.py`'s remote-install tests mock `install_linux`/
  `install_windows` entirely and never exercised the real key-parsing
  code. Fixed by looking classes up via `getattr(paramiko, name, None)`
  and filtering out whichever don't exist on the installed version,
  plus a dedicated `test_remote_install.py` that calls the real
  paramiko key-parsing path (unlike test_app.py's mocked version) --
  including a regression test that simulates a paramiko version missing
  an attribute this module references, so the same class of bug against
  a *future* paramiko release would fail loudly in CI instead of only
  in someone's browser.
- `install_windows()` only ever tried WinRM over HTTPS/5986, matching
  `provisioning/inventory.ini.example`'s documented convention -- but a
  real Windows 10 box only had an HTTP/5985 listener (plain
  `Enable-PSRemoting` doesn't set up HTTPS unless someone explicitly
  configures a cert), so every real Windows remote install was refused
  outright. No test caught this either, for the same reason as the
  DSSKey bug: fully mocked. Fixed by trying HTTPS first, then falling
  back to HTTP automatically (`_WINRM_ENDPOINTS`), with a test that
  asserts both endpoints get tried in order and a second-endpoint
  success is returned rather than the first endpoint's failure winning.
- `remote_install_route()`'s `download_url` used `str(request.base_url)`
  -- the address *the admin's browser* reached the server on -- as the
  address the *target* should fetch its install bundle from. Those
  match by coincidence on the same machine (which is all any test here
  could exercise) but not in general, and Remote Install's entire
  premise is that the admin and the target are different machines --
  exactly the OOB-vs-in-band addressing split this project's whole
  network model exists to keep separate. Surfaced immediately testing
  against a real target on a different network than the admin session:
  the SSH/WinRM connection succeeded, but the target's own curl/
  Invoke-WebRequest to "download the bundle" timed out reaching an
  address that was never reachable from there. Fixed with an explicit
  `remote_install_server_url` setting (Settings -> Remote Install) that
  overrides the inferred address; falls back to `request.base_url` only
  when unset, which is fine for same-machine/loopback testing but not
  general use.
- `install_windows()`'s PowerShell script's `Invoke-WebRequest` call
  produced ~57MB of stderr for a single real ~51MB installer download --
  its progress bar has nowhere to render non-interactively over WinRM,
  so it serializes as one CLIXML progress record per chunk instead.
  Harmless to the install itself (exit code and stdout were both
  correct) but would have bloated every real remote-install response
  and made its stderr output useless for actually debugging a failure.
  Fixed with `$ProgressPreference = 'SilentlyContinue'` at the top of
  the script; confirmed zero stderr bytes on a clean run afterward.

**Still stubbed / not yet built:**
1. Agent auth is a shared-bearer-token scheme (one token per host,
   minted at `/install/agent-bundle` download time -- see auth.py and
   "Installation" below), not mTLS. mTLS with per-agent client certs is
   still the better long-term fit for what's effectively a benign C2
   channel (per-agent revocation, no bearer secret that leaks the whole
   fleet if one host is compromised) -- this was a deliberate smaller
   first step, not a final answer.
2. Dashboard accounts have only two roles (`admin` / `viewer`, see
   auth.py) -- no finer-grained permissions (e.g. an operator who can
   launch runs but not manage other accounts) and no audit log of who
   did what. Fine for a small team; revisit if that stops being true.
3. Config templating in the Ansible playbooks (currently a TODO comment
   — needs to render `config.yaml` per-host with the right OOB IP). Linux
   puppet hosts are provisioned this way (Ansible + systemd, no packaged
   binary) rather than via an installer — only the Windows side got a
   PyInstaller/Inno Setup treatment, matching what was actually asked
   for; a Linux equivalent wasn't built.
4. `smb_access.py`'s Linux `mount.cifs` path is implemented per the
   documented interface but has never been run — verify on a real Linux
   puppet host before trusting it.
5. Second-source verification: cross-checking each action module's
   artifact (email Message-ID, file hash, browser history entry, SMB
   access) against the *target* system's own log (mail server delivery
   log, file server access log, etc.), not just the agent's own report of
   what it did. Natural to build as part of the scoring harness's alert
   ingestion once real range infrastructure is in the loop.
6. The automated test suite (see "Testing" below) doesn't cover actually
   driving a real browser, LibreOffice, or SMB share -- CI would need a
   real LibreOffice install, a downloaded Chromium, and a real or
   loopback SMB share to exercise those, which is a heavier CI setup
   than was worth building for the initial suite. Those paths stay
   hand-verified for now (see each action module's entry above).

## Running the prototype locally (single machine, no real OOB yet)

Every action module now does something real, so local testing needs the
things they talk to: a debug mail server, LibreOffice, and (implicitly)
Windows' own SMB redirector for `smb_access` if you're pointing it at a
real UNC path.

```bash
# mail server stub, so email_send has something real to deliver to
python -m smtpd -c DebuggingServer -n 127.0.0.1:1025   # prints received mail to stdout

# server (server/run_dev.cmd does the same thing via the checked-in
# server/.venv, if you'd rather not activate a venv by hand)
cd server
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
# open http://localhost:8000/ui/ -- launch a run from there instead of
# curl below if you'd rather click than type

# in another shell, start a scenario run
curl -X POST localhost:8000/runs -H 'Content-Type: application/json' \
  -d '{"scenario_name": "finance_analyst", "hosts": ["FIN-WKS03"], "seed": 42}'

# agent (in a third shell) -- agent/.venv is checked in with playwright
# already installed; you still need `playwright install chromium` once
# (downloads to a shared user-level cache, not the venv) and a real
# LibreOffice install (winget install TheDocumentFoundation.LibreOffice
# on Windows) for office_doc to have something to drive.
cd agent
pip install -r requirements.txt
playwright install chromium
cp config.yaml.example config.yaml   # edit host_id to match FIN-WKS03, drop oob_source_ip for local testing
python agent.py --config config.yaml
```

You should see the agent poll, pick up the four actions from
`scenarios/finance_analyst.yaml`, and actually execute all of them: a
real Chromium browse, a real SMTP send to the debug server above, a real
LibreOffice edit/save (watch for a `soffice.bin` process appear and
disappear in Task Manager), and a real file copy over SMB/UNC (point
`params['share']` at something reachable, or use Windows' pre-existing
`\\localhost\C$\...` admin share for a quick local test without setting
up a real file server). Watch it happen live at
`http://localhost:8000/ui/`, or check `GET localhost:8000/runs/{run_id}/ledger`
directly.
