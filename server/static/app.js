// Thin vanilla-JS client over the CyberSim orchestrator API. No build step,
// no CDN dependencies (this may run in an air-gapped range) -- served
// as-is by FastAPI's StaticFiles mount at /ui/. All API calls use
// absolute paths since the API lives at the server root, not under /ui/.

const $ = (id) => document.getElementById(id);

let selectedRunId = null;
let autoRefreshTimer = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
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

// ---- health -------------------------------------------------------------

async function checkHealth() {
  const pill = $("health");
  try {
    await api("/health");
    pill.textContent = "server reachable";
    pill.className = "pill pill-ok";
  } catch {
    pill.textContent = "server unreachable";
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

async function loadRuns() {
  const { runs } = await api("/runs");
  const tbody = document.querySelector("#runs-table tbody");
  tbody.innerHTML = runs
    .map(
      (r) =>
        `<tr class="clickable ${r.run_id === selectedRunId ? "selected" : ""}" data-run-id="${
          r.run_id
        }"><td title="${r.run_id}">${r.run_id.slice(0, 8)}</td><td>${r.scenario_name}</td><td>${
          r.seed
        }</td><td>${fmtTime(r.started_at)}</td></tr>`
    )
    .join("") || `<tr><td colspan="4" class="hint">no runs yet</td></tr>`;

  tbody.querySelectorAll("tr[data-run-id]").forEach((row) => {
    row.addEventListener("click", () => selectRun(row.dataset.runId));
  });
}

function selectRun(runId) {
  selectedRunId = runId;
  $("ledger-run-id").textContent = `(${runId.slice(0, 8)})`;
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
    return;
  }

  const entries = Object.values(ledger).sort(
    (a, b) => new Date(a.spec.intended_start) - new Date(b.spec.intended_start)
  );

  if (!entries.length) {
    empty.textContent = "this run has no actions.";
    empty.style.display = "block";
    table.style.display = "none";
    return;
  }
  empty.style.display = "none";
  table.style.display = "table";

  const tbody = document.querySelector("#ledger-table tbody");
  tbody.innerHTML = entries
    .map((e) => {
      const status = actionStatus(e);
      const artifact = e.completion
        ? JSON.stringify(e.completion.observed_side_effects || {}).slice(0, 60)
        : "—";
      return `<tr>
        <td title="${e.spec.action_id}">${e.spec.action_id.slice(0, 8)}</td>
        <td>${e.spec.action_type}${e.spec.should_alert ? " ⚠️" : ""}</td>
        <td>${e.spec.host}</td>
        <td>${fmtTime(e.spec.intended_start)}</td>
        <td class="${statusClass(status)}">${status}</td>
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

  $("launch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const result = $("launch-result");
    const submitBtn = e.target.querySelector("button[type=submit]");

    const hosts = $("hosts-input")
      .value.split(",")
      .map((h) => h.trim())
      .filter(Boolean);
    if (!hosts.length) {
      result.textContent = "enter at least one host";
      result.className = "result error";
      return;
    }

    const payload = {
      scenario_name: $("scenario-select").value,
      hosts,
    };
    const seed = $("seed-input").value;
    if (seed !== "") payload.seed = parseInt(seed, 10);
    const start = $("start-input").value;
    if (start) payload.start_time = new Date(start).toISOString();

    submitBtn.disabled = true;
    try {
      const res = await api("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      result.textContent = `launched run ${res.run_id} — seed ${res.seed}, ${res.action_count} actions`;
      result.className = "result success";
      await loadRuns();
      selectRun(res.run_id);
    } catch (err) {
      result.textContent = `launch failed: ${err.message}`;
      result.className = "result error";
    } finally {
      submitBtn.disabled = false;
    }
  });
}

function setupAutoRefresh() {
  const toggle = $("autorefresh-toggle");
  const tick = () => {
    if (toggle.checked && selectedRunId) loadLedger();
    loadAgents();
    loadRuns();
  };
  autoRefreshTimer = setInterval(tick, 3000);
}

// ---- init -------------------------------------------------------------------

(async function init() {
  setupLaunchForm();
  setupAutoRefresh();
  await Promise.all([checkHealth(), loadScenarios(), loadAgents(), loadRuns()]);
  setInterval(checkHealth, 10000);
})();
