// Thin vanilla-JS client over the CyberSim orchestrator API. No build step,
// no CDN dependencies (this may run in an air-gapped range) -- served
// as-is by FastAPI's StaticFiles mount at /ui/. All API calls use
// absolute paths since the API lives at the server root, not under /ui/.

const $ = (id) => document.getElementById(id);

let selectedRunId = null;
let autoRefreshTimer = null;
let currentUser = null;

// ---- role gating ------------------------------------------------------------

function applyRoleGating() {
  const isAdmin = currentUser?.role === "admin";
  $("nav-scenario-builder").style.display = isAdmin ? "" : "none";
  $("nav-install").style.display = isAdmin ? "" : "none";
  $("nav-users").style.display = isAdmin ? "" : "none";

  if (!isAdmin) {
    const submitBtn = $("launch-submit-btn");
    submitBtn.disabled = true;
    submitBtn.title = "viewer accounts can't launch runs";
    $("repeat-toggle").disabled = true;
    const result = $("launch-result");
    result.textContent = "Viewing only — launching runs requires an admin account.";
    result.className = "result error";
  }
}

async function api(path, opts) {
  const res = await authedFetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      /* ignore -- non-JSON error body */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.status === 204 ? null : res.json();
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// ---- onboarding -----------------------------------------------------------

function setupOnboarding() {
  const ONBOARDING_KEY = "cybersim-onboarding-dismissed";

  $("onboarding-dismiss").addEventListener("click", () => {
    localStorage.setItem(ONBOARDING_KEY, "1");
    document.documentElement.classList.add("onboarding-dismissed");
  });
  $("onboarding-reopen").addEventListener("click", () => {
    localStorage.removeItem(ONBOARDING_KEY);
    document.documentElement.classList.remove("onboarding-dismissed");
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("onboarding-card").scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
  });
}

// ---- health -------------------------------------------------------------

async function checkHealth() {
  const pill = $("health");
  try {
    await api("/health");
    pill.innerHTML = `${iconHtml("success")} server reachable`;
    pill.className = "pill pill-ok";
  } catch {
    pill.innerHTML = `${iconHtml("failure")} server unreachable`;
    pill.className = "pill pill-bad";
  }
}

// ---- scenarios ------------------------------------------------------------

async function loadScenarios() {
  const select = $("scenario-select");
  const { scenarios } = await api("/scenarios");
  select.innerHTML = scenarios.map((s) => `<option value="${s}">${s}</option>`).join("");
  if (scenarios.length) await loadScenarioPreview(scenarios[0]);
}

async function loadScenarioPreview(name) {
  const preview = $("scenario-preview");
  if (!name) {
    preview.textContent = "";
    return;
  }
  try {
    const scenario = await api(`/scenarios/${encodeURIComponent(name)}`);
    const lines = (scenario.schedule || []).map((step) => {
      const flag = step.should_alert ? " [RED TEAM]" : "";
      const cls = step.should_alert ? "alert-step" : "";
      return `<div class="${cls}">${step.action}${flag} — delay ${step.delay_before || "0s"}${
        step.duration ? `, duration ${step.duration}` : ""
      }</div>`;
    });
    preview.innerHTML = `<strong>${scenario.persona}</strong>` + lines.join("");
  } catch (err) {
    preview.textContent = `couldn't load preview: ${err.message}`;
  }
}

// ---- live topology ----------------------------------------------------------

async function loadTopology() {
  try {
    const { agents } = await api("/agents/live-status");
    renderTopology(agents);
  } catch {
    /* transient poll failure -- next tick will retry, no need to surface it */
  }
}

// ---- agents ---------------------------------------------------------------

async function loadAgents() {
  const { agents } = await api("/agents");

  const tbody = document.querySelector("#agents-table tbody");
  tbody.innerHTML = agents
    .map(
      (a) =>
        `<tr><td>${a.host}</td><td>${a.os}</td><td>${a.persona || "—"}</td><td>${fmtTime(
          a.last_seen
        )}</td></tr>`
    )
    .join("") || `<tr><td colspan="4" class="hint">no agents have registered yet</td></tr>`;

  const chipRow = $("known-hosts");
  chipRow.innerHTML = agents
    .map((a) => `<button type="button" class="chip" data-host="${a.host}">${a.host}</button>`)
    .join("");
  chipRow.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const input = $("hosts-input");
      const current = input.value
        .split(",")
        .map((h) => h.trim())
        .filter(Boolean);
      const host = chip.dataset.host;
      if (!current.includes(host)) current.push(host);
      input.value = current.join(", ");
    });
  });
}

// ---- runs / ledger ----------------------------------------------------------

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

let allRuns = [];

function matchesRunsFilter(run, filter) {
  return run.scenario_name.toLowerCase().includes(filter) || run.run_id.toLowerCase().includes(filter);
}

function renderRunsTable() {
  const filter = $("runs-filter-input").value.trim().toLowerCase();
  const runs = filter ? allRuns.filter((r) => matchesRunsFilter(r, filter)) : allRuns;

  const tbody = document.querySelector("#runs-table tbody");
  tbody.innerHTML =
    runs
      .map(
        (r) =>
          `<tr class="clickable ${r.run_id === selectedRunId ? "selected" : ""}" data-run-id="${
            r.run_id
          }" tabindex="0" role="button" aria-label="View ledger for run ${r.run_id}"><td title="${
            r.run_id
          }">${r.run_id.slice(0, 8)}</td><td>${r.scenario_name}</td><td>${
            r.seed
          }</td><td>${fmtTime(r.started_at)}</td></tr>`
      )
      .join("") ||
    (allRuns.length
      ? `<tr><td colspan="4" class="hint">no runs match "${escapeHtml(filter)}"</td></tr>`
      : `<tr><td colspan="4" class="hint">no runs yet</td></tr>`);

  tbody.querySelectorAll("tr[data-run-id]").forEach((row) => {
    row.addEventListener("click", () => selectRun(row.dataset.runId));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectRun(row.dataset.runId);
      }
    });
  });
}

async function loadRuns() {
  const { runs } = await api("/runs");
  allRuns = runs;
  renderRunsTable();
}

function setupRunsFilter() {
  $("runs-filter-input").addEventListener("input", renderRunsTable);
}

function selectRun(runId) {
  selectedRunId = runId;
  $("ledger-run-id").textContent = `(${runId.slice(0, 8)})`;
  $("scoring-run-id").textContent = `(${runId.slice(0, 8)})`;
  $("scoring-result").textContent = "";
  $("scoring-result").className = "result";
  $("scoring-report").innerHTML = "";
  document.querySelectorAll("#runs-table tbody tr[data-run-id]").forEach((row) => {
    row.classList.toggle("selected", row.dataset.runId === runId);
  });
  loadLedger();
}

function actionStatus(entry) {
  if (entry.completion) return entry.completion.exit_status; // success | failure | partial
  if (entry.intent) return "in progress";
  return "pending";
}

function statusClass(status) {
  if (status === "success") return "status-success";
  if (status === "failure") return "status-failure";
  if (status === "partial") return "status-partial";
  if (status === "in progress") return "status-progress";
  return "status-pending";
}

async function loadLedger() {
  if (!selectedRunId) return;
  const empty = $("ledger-empty");
  const table = $("ledger-table");

  let ledger;
  try {
    ledger = await api(`/runs/${selectedRunId}/ledger`);
  } catch (err) {
    empty.textContent = `couldn't load ledger: ${err.message}`;
    empty.style.display = "block";
    table.style.display = "none";
    renderGantt({});
    return;
  }

  const entries = Object.values(ledger).sort(
    (a, b) => new Date(a.spec.intended_start) - new Date(b.spec.intended_start)
  );

  if (!entries.length) {
    empty.textContent = "this run has no actions.";
    empty.style.display = "block";
    table.style.display = "none";
    renderGantt({});
    return;
  }
  empty.style.display = "none";
  table.style.display = "table";
  renderGantt(ledger);

  const tbody = document.querySelector("#ledger-table tbody");
  tbody.innerHTML = entries
    .map((e) => {
      const status = actionStatus(e);
      const artifact = e.completion
        ? JSON.stringify(e.completion.observed_side_effects || {}).slice(0, 60)
        : "—";
      return `<tr data-action-id="${e.spec.action_id}">
        <td title="${e.spec.action_id}">${e.spec.action_id.slice(0, 8)}</td>
        <td>${e.spec.action_type}${e.spec.should_alert ? iconHtml("alert", "icon-alert-inline") : ""}</td>
        <td>${e.spec.host}</td>
        <td>${fmtTime(e.spec.intended_start)}</td>
        <td class="${statusClass(status)} status-cell">${statusIconHtml(status)}${status}</td>
        <td class="hint">${artifact}</td>
      </tr>`;
    })
    .join("");

  const done = entries.filter((e) => e.completion).length;
  $("ledger-progress-bar").style.width = `${Math.round((done / entries.length) * 100)}%`;
}

// ---- launch form ----------------------------------------------------------

function setupLaunchForm() {
  $("scenario-select").addEventListener("change", (e) => loadScenarioPreview(e.target.value));

  const repeatToggle = $("repeat-toggle");
  const repeatRow = $("repeat-interval-row");
  const submitBtn = $("launch-submit-btn");
  repeatToggle.addEventListener("change", () => {
    repeatRow.style.display = repeatToggle.checked ? "" : "none";
    submitBtn.textContent = repeatToggle.checked ? "Schedule recurring run" : "Launch run";
  });

  $("launch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const result = $("launch-result");

    const hosts = $("hosts-input")
      .value.split(",")
      .map((h) => h.trim())
      .filter(Boolean);
    if (!hosts.length) {
      result.textContent = "enter at least one host";
      result.className = "result error";
      return;
    }

    const scenario_name = $("scenario-select").value;
    const seed = $("seed-input").value;
    const start = $("start-input").value;
    const start_time = start ? new Date(start).toISOString() : undefined;

    submitBtn.disabled = true;
    try {
      if (repeatToggle.checked) {
        const intervalCount = parseFloat($("repeat-interval-input").value);
        const intervalUnitSeconds = parseInt($("repeat-interval-unit").value, 10);
        const interval_seconds = Math.round(intervalCount * intervalUnitSeconds);
        if (!(interval_seconds >= 60)) {
          result.textContent = "repeat interval must be at least 1 minute";
          result.className = "result error";
          return;
        }
        const payload = { scenario_name, hosts, interval_seconds };
        if (seed !== "") payload.seed = parseInt(seed, 10);
        if (start_time) payload.start_time = start_time;

        const res = await api("/schedules", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        result.textContent = `scheduled — first fire ${fmtTime(res.next_run_at)}, every ${intervalCount} ${
          $("repeat-interval-unit").selectedOptions[0].textContent
        }`;
        result.className = "result success";
        await loadSchedules();
      } else {
        const payload = { scenario_name, hosts };
        if (seed !== "") payload.seed = parseInt(seed, 10);
        if (start_time) payload.start_time = start_time;

        const res = await api("/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        result.textContent = `launched run ${res.run_id} — seed ${res.seed}, ${res.action_count} actions`;
        result.className = "result success";
        await loadRuns();
        selectRun(res.run_id);
      }
    } catch (err) {
      result.textContent = `${repeatToggle.checked ? "scheduling" : "launch"} failed: ${err.message}`;
      result.className = "result error";
    } finally {
      submitBtn.disabled = false;
    }
  });
}

// ---- scheduled runs ---------------------------------------------------------

function fmtInterval(seconds) {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

async function loadSchedules() {
  const { schedules } = await api("/schedules");
  const empty = $("schedules-empty");
  const table = $("schedules-table");

  if (!schedules.length) {
    empty.style.display = "block";
    table.style.display = "none";
    return;
  }
  empty.style.display = "none";
  table.style.display = "table";

  const isAdmin = currentUser?.role === "admin";
  const tbody = document.querySelector("#schedules-table tbody");
  tbody.innerHTML = schedules
    .map(
      (s) => `<tr data-schedule-id="${s.schedule_id}" class="${s.enabled ? "" : "hint"}">
        <td>${s.scenario_name}</td>
        <td>${s.hosts.join(", ")}</td>
        <td>${fmtInterval(s.interval_seconds)}</td>
        <td>${s.enabled ? fmtTime(s.next_run_at) : "paused"}</td>
        <td>${
          isAdmin
            ? `<button type="button" class="chip" data-toggle-schedule="${s.schedule_id}" data-enabled="${s.enabled}">${
                s.enabled ? "pause" : "resume"
              }</button>`
            : ""
        }</td>
        <td>${
          isAdmin
            ? `<button type="button" class="chip" data-delete-schedule="${s.schedule_id}">${iconHtml(
                "failure"
              )} cancel</button>`
            : ""
        }</td>
      </tr>`
    )
    .join("");
}

function setupSchedulesTable() {
  document.querySelector("#schedules-table tbody").addEventListener("click", async (e) => {
    const toggleBtn = e.target.closest("[data-toggle-schedule]");
    const deleteBtn = e.target.closest("[data-delete-schedule]");
    if (toggleBtn) {
      const id = toggleBtn.dataset.toggleSchedule;
      const enabled = toggleBtn.dataset.enabled === "true";
      toggleBtn.disabled = true;
      try {
        await api(`/schedules/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !enabled }),
        });
        await loadSchedules();
      } catch {
        toggleBtn.disabled = false;
      }
    } else if (deleteBtn) {
      deleteBtn.disabled = true;
      try {
        await api(`/schedules/${deleteBtn.dataset.deleteSchedule}`, { method: "DELETE" });
        await loadSchedules();
      } catch {
        deleteBtn.disabled = false;
      }
    }
  });
}

function setupAutoRefresh() {
  const toggle = $("autorefresh-toggle");
  const tick = () => {
    if (toggle.checked && selectedRunId) loadLedger();
    loadAgents();
    loadRuns();
    loadTopology();
    loadSchedules();
  };
  autoRefreshTimer = setInterval(tick, 3000);
}

// ---- init -------------------------------------------------------------------

(async function init() {
  currentUser = await setupWhoami();
  applyRoleGating();
  setupOnboarding();
  setupLaunchForm();
  setupSchedulesTable();
  setupRunsFilter();
  setupAutoRefresh();
  loadTopology();
  loadSchedules();
  await Promise.all([checkHealth(), loadScenarios(), loadAgents(), loadRuns()]);
  setInterval(checkHealth, 10000);
})();
