// Ranges page: create/list/manage multi-day, business-hours-window
// ranges (see DEVELOPER_NOTES.md "Ranges" and scenario_engine.
// resolve_window()). Self-contained script -- doesn't load app.js, same
// convention as settings.js/scenario-builder.js each having their own
// small api()/escapeHtml() rather than a shared module.

const $ = (id) => document.getElementById(id);

let currentUser = null;

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

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// ---- role gating ------------------------------------------------------

function applyRoleGating() {
  const isAdmin = currentUser?.role === "admin";
  if (!isAdmin) {
    $("create-range-card").style.display = "none";
    $("viewer-blocked-card").style.display = "";
  }
}

// ---- scenario org/persona cascade (for the "add host" mini-form) ------
// Self-contained copy of app.js's launch-form cascade -- this page
// doesn't load app.js, same "each page owns its own script" convention
// as api()/escapeHtml() above.

const ALL_ROLES_ORG = "__all__";
let allScenarioNames = [];
let scenarioOrgs = [];

async function loadScenarioOptions() {
  const { scenarios, orgs } = await api("/scenarios");
  allScenarioNames = scenarios;
  scenarioOrgs = orgs;

  const orgSelect = $("range-scenario-org-select");
  const orgOptions = orgs.map((o) => `<option value="${escapeHtml(o.org)}">${escapeHtml(o.org)}</option>`);
  orgOptions.push(`<option value="${ALL_ROLES_ORG}">All roles</option>`);
  orgSelect.innerHTML = orgOptions.join("");
  orgSelect.value = orgs.length ? orgs[0].org : ALL_ROLES_ORG;

  populateScenarioSelectForOrg(orgSelect.value);
}

function populateScenarioSelectForOrg(orgName) {
  const select = $("range-scenario-select");
  if (orgName === ALL_ROLES_ORG || !orgName) {
    select.innerHTML = allScenarioNames.map((s) => `<option value="${s}">${s}</option>`).join("");
    return;
  }
  const org = scenarioOrgs.find((o) => o.org === orgName);
  const departments = org ? org.departments : [];
  select.innerHTML = departments
    .map(
      (d) =>
        `<optgroup label="${escapeHtml(d.department)}">` +
        d.roles.map((r) => `<option value="${r.name}">${escapeHtml(r.persona)}</option>`).join("") +
        `</optgroup>`
    )
    .join("");
}

// ---- known hosts (click to fill the host input) ------------------------

async function loadKnownHosts() {
  const { agents } = await api("/agents");
  const chipRow = $("range-known-hosts");
  chipRow.innerHTML = agents.map((a) => `<button type="button" class="chip" data-host="${a.host}">${a.host}</button>`).join("");
  chipRow.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      $("range-host-input").value = chip.dataset.host;
    });
  });
}

// ---- pending hosts list (host, scenario_name) pairs before creating ----

let pendingHosts = [];

function renderPendingHostsTable() {
  const table = $("range-hosts-table");
  const empty = $("range-hosts-empty");
  if (!pendingHosts.length) {
    table.style.display = "none";
    empty.style.display = "block";
    return;
  }
  table.style.display = "table";
  empty.style.display = "none";
  const tbody = document.querySelector("#range-hosts-table tbody");
  tbody.innerHTML = pendingHosts
    .map(
      (h, i) =>
        `<tr><td>${escapeHtml(h.host)}</td><td>${escapeHtml(h.scenario_name)}</td>` +
        `<td><button type="button" class="chip" data-remove-host="${i}">${iconHtml("failure")} remove</button></td></tr>`
    )
    .join("");
  tbody.querySelectorAll("[data-remove-host]").forEach((btn) => {
    btn.addEventListener("click", () => {
      pendingHosts.splice(parseInt(btn.dataset.removeHost, 10), 1);
      renderPendingHostsTable();
    });
  });
}

function setupHostBuilder() {
  $("range-scenario-org-select").addEventListener("change", (e) => populateScenarioSelectForOrg(e.target.value));

  $("range-add-host-btn").addEventListener("click", () => {
    const result = $("range-form-result");
    const host = $("range-host-input").value.trim();
    const scenario_name = $("range-scenario-select").value;
    if (!host) {
      result.textContent = "enter a host name first";
      result.className = "result error";
      return;
    }
    if (pendingHosts.some((h) => h.host === host)) {
      result.textContent = `${host} is already in the list`;
      result.className = "result error";
      return;
    }
    pendingHosts.push({ host, scenario_name });
    $("range-host-input").value = "";
    result.textContent = "";
    result.className = "result";
    renderPendingHostsTable();
  });
}

// ---- create-range form --------------------------------------------------

function setupRangeForm() {
  $("range-compressed-toggle").addEventListener("change", (e) => {
    $("range-time-scale-row").style.display = e.target.checked ? "" : "none";
  });
  $("range-injection-mode-select").addEventListener("change", (e) => {
    $("range-injection-probability-row").style.display = e.target.value === "auto" ? "" : "none";
  });

  $("range-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const result = $("range-form-result");
    const submitBtn = $("range-submit-btn");

    if (!pendingHosts.length) {
      result.textContent = "add at least one host before creating the range";
      result.className = "result error";
      return;
    }

    const payload = {
      name: $("range-name-input").value,
      start_date: $("range-start-date-input").value,
      num_days: parseInt($("range-num-days-input").value, 10),
      window_start_local: $("range-window-start-input").value,
      window_end_local: $("range-window-end-input").value,
      timezone: $("range-timezone-input").value,
      injection_mode: $("range-injection-mode-select").value,
      hosts: pendingHosts,
    };
    if ($("range-compressed-toggle").checked) {
      payload.time_scale = parseFloat($("range-time-scale-input").value);
    }
    if (payload.injection_mode === "auto") {
      payload.injection_probability = parseFloat($("range-injection-probability-input").value);
    }
    const seed = $("range-seed-input").value;
    if (seed !== "") payload.seed = parseInt(seed, 10);

    submitBtn.disabled = true;
    try {
      const created = await api("/ranges", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      result.textContent = `range "${created.name}" created — ${pendingHosts.length} host(s), first day starts ${fmtTime(created.next_day_launch_at)}`;
      result.className = "result success";
      pendingHosts = [];
      renderPendingHostsTable();
      e.target.reset();
      $("range-window-start-input").value = "08:00";
      $("range-window-end-input").value = "16:00";
      $("range-timezone-input").value = "UTC";
      await loadRanges();
      selectRange(created.range_id);
    } catch (err) {
      result.textContent = `couldn't create range: ${err.message}`;
      result.className = "result error";
    } finally {
      submitBtn.disabled = false;
    }
  });
}

// ---- ranges list ----------------------------------------------------------

let selectedRangeId = null;

function dayStatus(r) {
  if (!r.enabled && r.current_day_index >= r.num_days) return "done";
  if (!r.enabled) return "paused";
  return `day ${Math.min(r.current_day_index + 1, r.num_days)}/${r.num_days}`;
}

async function loadRanges() {
  const { ranges } = await api("/ranges");
  const empty = $("ranges-empty");
  const table = $("ranges-table");

  if (!ranges.length) {
    empty.style.display = "block";
    table.style.display = "none";
    return;
  }
  empty.style.display = "none";
  table.style.display = "table";

  const isAdmin = currentUser?.role === "admin";
  const tbody = document.querySelector("#ranges-table tbody");
  tbody.innerHTML = ranges
    .map((r) => {
      const done = !r.enabled && r.current_day_index >= r.num_days;
      return `<tr class="clickable ${r.enabled ? "" : "hint"} ${r.range_id === selectedRangeId ? "selected" : ""}" data-range-id="${r.range_id}" tabindex="0" role="button">
        <td>${escapeHtml(r.name)}</td>
        <td>${dayStatus(r)}</td>
        <td>${r.injection_mode}</td>
        <td>${r.enabled ? fmtTime(r.next_day_launch_at) : "—"}</td>
        <td>${
          isAdmin && !done
            ? `<button type="button" class="chip" data-toggle-range="${r.range_id}" data-enabled="${r.enabled}">${r.enabled ? "pause" : "resume"}</button>`
            : ""
        }</td>
        <td>${isAdmin ? `<button type="button" class="chip" data-delete-range="${r.range_id}">${iconHtml("failure")} cancel</button>` : ""}</td>
      </tr>`;
    })
    .join("");

  tbody.querySelectorAll("tr[data-range-id]").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("button")) return; // pause/resume/cancel handle their own clicks
      selectRange(row.dataset.rangeId);
    });
  });
}

function setupRangesTable() {
  document.querySelector("#ranges-table tbody").addEventListener("click", async (e) => {
    const toggleBtn = e.target.closest("[data-toggle-range]");
    const deleteBtn = e.target.closest("[data-delete-range]");
    if (toggleBtn) {
      const id = toggleBtn.dataset.toggleRange;
      const enabled = toggleBtn.dataset.enabled === "true";
      toggleBtn.disabled = true;
      try {
        await api(`/ranges/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !enabled }),
        });
        await loadRanges();
      } catch {
        toggleBtn.disabled = false;
      }
    } else if (deleteBtn) {
      deleteBtn.disabled = true;
      try {
        await api(`/ranges/${deleteBtn.dataset.deleteRange}`, { method: "DELETE" });
        if (deleteBtn.dataset.deleteRange === selectedRangeId) {
          selectedRangeId = null;
          $("range-detail-card").style.display = "none";
        }
        await loadRanges();
      } catch {
        deleteBtn.disabled = false;
      }
    }
  });
}

// ---- range detail (hosts, injections, add-injection form) --------------

let suspiciousBehaviors = null;

async function loadSuspiciousBehaviorsOnce() {
  if (!suspiciousBehaviors) {
    const { behaviors } = await api("/suspicious-behaviors");
    suspiciousBehaviors = behaviors;
  }
  return suspiciousBehaviors;
}

async function selectRange(rangeId) {
  selectedRangeId = rangeId;
  document.querySelectorAll("#ranges-table tbody tr[data-range-id]").forEach((row) => {
    row.classList.toggle("selected", row.dataset.rangeId === rangeId);
  });

  const detail = await api(`/ranges/${rangeId}`);
  $("range-detail-card").style.display = "";
  $("range-detail-name").textContent = `(${escapeHtml(detail.name)} — ${dayStatus(detail)})`;

  document.querySelector("#range-detail-hosts-table tbody").innerHTML = detail.hosts
    .map((h) => `<tr><td>${escapeHtml(h.host)}</td><td>${escapeHtml(h.scenario_name)}</td></tr>`)
    .join("");

  const injEmpty = $("range-detail-injections-empty");
  const injTable = $("range-detail-injections-table");
  if (!detail.injections.length) {
    injTable.style.display = "none";
    injEmpty.style.display = "block";
  } else {
    injTable.style.display = "table";
    injEmpty.style.display = "none";
    document.querySelector("#range-detail-injections-table tbody").innerHTML = detail.injections
      .map(
        (i) =>
          `<tr><td>${i.day_index}</td><td>${escapeHtml(i.host)}</td><td>${escapeHtml(i.behavior_id)}</td><td>${i.created_by}</td></tr>`
      )
      .join("");
  }

  const isAdmin = currentUser?.role === "admin";
  const canAddManual = isAdmin && detail.injection_mode === "manual" && detail.enabled;
  $("range-add-injection-block").style.display = canAddManual ? "" : "none";
  if (canAddManual) {
    $("injection-host-select").innerHTML = detail.hosts.map((h) => `<option value="${escapeHtml(h.host)}">${escapeHtml(h.host)}</option>`).join("");
    $("injection-day-input").max = detail.num_days - 1;
    const behaviors = await loadSuspiciousBehaviorsOnce();
    $("injection-behavior-select").innerHTML = behaviors
      .map((b) => `<option value="${b.id}">${escapeHtml(b.label)} (${b.category})</option>`)
      .join("");
  }
}

function setupInjectionForm() {
  $("range-injection-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!selectedRangeId) return;
    const result = $("injection-result");
    const submitBtn = $("injection-submit-btn");

    const payload = {
      host: $("injection-host-select").value,
      day_index: parseInt($("injection-day-input").value, 10),
      behavior_id: $("injection-behavior-select").value,
    };

    submitBtn.disabled = true;
    try {
      await api(`/ranges/${selectedRangeId}/injections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      result.textContent = "injection added";
      result.className = "result success";
      await selectRange(selectedRangeId);
    } catch (err) {
      result.textContent = `couldn't add injection: ${err.message}`;
      result.className = "result error";
    } finally {
      submitBtn.disabled = false;
    }
  });
}

// ---- init -------------------------------------------------------------

(async function init() {
  currentUser = await setupWhoami();
  if (!currentUser) return;
  applyRoleGating();
  setupHostBuilder();
  setupRangeForm();
  setupRangesTable();
  setupInjectionForm();
  await Promise.all([loadScenarioOptions(), loadKnownHosts(), loadRanges()]);
})();
