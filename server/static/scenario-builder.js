// Scenario builder: a form over POST /scenarios so authoring a scenario
// doesn't require hand-editing YAML on the server's filesystem. State
// lives in a plain JS array of step objects (one per schedule step);
// re-rendering the step cards on every keystroke would blow away input
// focus, so field edits mutate state in place and only re-render the
// (cheap, text-only) preview -- a full re-render of #steps-root only
// happens on structural changes (action type switch, add/remove step).

const ACTION_TYPES = ["web_browse", "email_send", "office_doc", "smb_access"];
const DURATION_UNITS = ["s", "m", "h"];

function defaultStep() {
  return {
    action: "web_browse",
    delayLo: 0,
    delayHi: 0,
    delayUnit: "s",
    hasDuration: false,
    durationLo: 0,
    durationHi: 0,
    durationUnit: "s",
    shouldAlert: false,
    targets: "",
    to: "",
    template: "",
    app: "",
    file: "",
    docOps: "",
    share: "",
    smbOps: "",
    expectedArtifacts: "",
    advancedJson: "",
  };
}

const state = { steps: [defaultStep()] };

function escapeAttr(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function splitCsv(s) {
  return String(s ?? "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

function actionFieldsHtml(step, i) {
  switch (step.action) {
    case "web_browse":
      return `<label>Targets <span class="hint">(comma-separated URLs — one is chosen at random each run)</span>
        <input type="text" value="${escapeAttr(step.targets)}" data-field="targets" data-index="${i}"
          placeholder="http://intranet.corp.local, http://finance-portal.corp.local"></label>`;
    case "email_send":
      return `<div class="row">
        <label>To <span class="hint">(puppet recipient address)</span>
          <input type="text" value="${escapeAttr(step.to)}" data-field="to" data-index="${i}" placeholder="hr-puppet@corp.local"></label>
        <label>Template
          <input type="text" value="${escapeAttr(step.template)}" data-field="template" data-index="${i}" placeholder="monthly_report"></label>
      </div>`;
    case "office_doc":
      return `<div class="row">
        <label>App
          <input type="text" value="${escapeAttr(step.app)}" data-field="app" data-index="${i}" placeholder="libreoffice_calc"></label>
        <label>File
          <input type="text" value="${escapeAttr(step.file)}" data-field="file" data-index="${i}" placeholder="budget_q3.xlsx"></label>
      </div>
      <label>Operations <span class="hint">(comma-separated, in order)</span>
        <input type="text" value="${escapeAttr(step.docOps)}" data-field="docOps" data-index="${i}" placeholder="open, edit_cells, save, close"></label>`;
    case "smb_access":
      return `<label>Share <span class="hint">(UNC path)</span>
        <input type="text" value="${escapeAttr(step.share)}" data-field="share" data-index="${i}" placeholder="\\\\fileserver01\\finance"></label>
      <label>Operations <span class="hint">(comma-separated)</span>
        <input type="text" value="${escapeAttr(step.smbOps)}" data-field="smbOps" data-index="${i}" placeholder="browse, copy_file"></label>`;
    default:
      return "";
  }
}

function stepCardHtml(step, i, removable) {
  return `<fieldset class="step-card" data-index="${i}">
    <div class="step-card-header">
      <span class="step-number">step ${i + 1}</span>
      ${
        removable
          ? `<button type="button" class="step-remove" data-remove="${i}" aria-label="Remove step ${i + 1}">${iconHtml("failure")} remove</button>`
          : ""
      }
    </div>

    <label>
      Action type
      <select data-field="action" data-index="${i}">
        ${ACTION_TYPES.map((a) => `<option value="${a}" ${step.action === a ? "selected" : ""}>${a}</option>`).join("")}
      </select>
    </label>

    <label>Delay before <span class="hint">(random range)</span>
      <div class="range-input">
        <input type="number" min="0" step="any" value="${step.delayLo}" data-field="delayLo" data-index="${i}">
        <span>&ndash;</span>
        <input type="number" min="0" step="any" value="${step.delayHi}" data-field="delayHi" data-index="${i}">
        <select data-field="delayUnit" data-index="${i}">
          ${DURATION_UNITS.map((u) => `<option value="${u}" ${step.delayUnit === u ? "selected" : ""}>${u}</option>`).join("")}
        </select>
      </div>
    </label>

    <label class="checkbox-label">
      <input type="checkbox" data-field="hasDuration" data-index="${i}" ${step.hasDuration ? "checked" : ""}>
      Runs for a duration <span class="hint">(vs. instantaneous, like a single click)</span>
    </label>
    ${
      step.hasDuration
        ? `<label>Duration <span class="hint">(random range)</span>
      <div class="range-input">
        <input type="number" min="0" step="any" value="${step.durationLo}" data-field="durationLo" data-index="${i}">
        <span>&ndash;</span>
        <input type="number" min="0" step="any" value="${step.durationHi}" data-field="durationHi" data-index="${i}">
        <select data-field="durationUnit" data-index="${i}">
          ${DURATION_UNITS.map((u) => `<option value="${u}" ${step.durationUnit === u ? "selected" : ""}>${u}</option>`).join("")}
        </select>
      </div>
    </label>`
        : ""
    }

    <label class="checkbox-label">
      <input type="checkbox" data-field="shouldAlert" data-index="${i}" ${step.shouldAlert ? "checked" : ""}>
      ${iconHtml("alert", "icon-alert-inline")} Red team <span class="hint">(should trigger a detection alert)</span>
    </label>

    ${actionFieldsHtml(step, i)}

    <label>
      Expected artifacts <span class="hint">(comma-separated, optional — documents intent for the scoring harness)</span>
      <input type="text" value="${escapeAttr(step.expectedArtifacts)}" data-field="expectedArtifacts" data-index="${i}" placeholder="dns_query, tls_session">
    </label>

    <details class="step-advanced">
      <summary>Advanced: extra params (JSON)</summary>
      <textarea data-field="advancedJson" data-index="${i}" rows="2" placeholder='{"custom_key": "value"}'>${escapeAttr(step.advancedJson)}</textarea>
    </details>
  </fieldset>`;
}

function renderSteps() {
  const root = document.getElementById("steps-root");
  root.innerHTML = state.steps.map((step, i) => stepCardHtml(step, i, state.steps.length > 1)).join("");
}

function renderPreview() {
  const persona = document.getElementById("persona-input").value.trim() || "(persona)";
  const lines = state.steps.map((step) => {
    const flag = step.shouldAlert ? " [RED TEAM]" : "";
    const cls = step.shouldAlert ? "alert-step" : "";
    const delay = `${step.delayLo}-${step.delayHi}${step.delayUnit}`;
    const duration = step.hasDuration ? `, duration ${step.durationLo}-${step.durationHi}${step.durationUnit}` : "";
    return `<div class="${cls}">${step.action}${flag} — delay ${delay}${duration}</div>`;
  });
  document.getElementById("scenario-preview").innerHTML = `<strong>${escapeAttr(persona)}</strong>` + lines.join("");
}

function knownParams(step) {
  let raw;
  switch (step.action) {
    case "email_send":
      raw = { to: step.to.trim(), template: step.template.trim() };
      break;
    case "office_doc":
      raw = { app: step.app.trim(), file: step.file.trim(), ops: splitCsv(step.docOps) };
      break;
    case "smb_access":
      raw = { share: step.share.trim(), ops: splitCsv(step.smbOps) };
      break;
    default:
      raw = {};
  }
  return Object.fromEntries(
    Object.entries(raw).filter(([, v]) => v !== "" && !(Array.isArray(v) && v.length === 0))
  );
}

function buildStepPayload(step) {
  let extraParams = {};
  if (step.advancedJson.trim()) {
    try {
      extraParams = JSON.parse(step.advancedJson);
    } catch {
      throw new Error(`invalid JSON in advanced params for a "${step.action}" step`);
    }
    if (typeof extraParams !== "object" || Array.isArray(extraParams) || extraParams === null) {
      throw new Error(`advanced params for a "${step.action}" step must be a JSON object`);
    }
  }

  return {
    action: step.action,
    delay_before: `${step.delayLo}-${step.delayHi}${step.delayUnit}`,
    duration: step.hasDuration ? `${step.durationLo}-${step.durationHi}${step.durationUnit}` : null,
    should_alert: step.shouldAlert,
    targets: step.action === "web_browse" ? splitCsv(step.targets) : [],
    params: { ...knownParams(step), ...extraParams },
    expected_artifacts: splitCsv(step.expectedArtifacts),
  };
}

async function postScenario(body, overwrite) {
  const res = await authedFetch(`/scenarios${overwrite ? "?overwrite=true" : ""}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    const err = new Error(errBody.detail || res.statusText);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function handleSubmit(e) {
  e.preventDefault();
  const result = document.getElementById("builder-result");
  const name = document.getElementById("scenario-name-input").value.trim();
  const persona = document.getElementById("persona-input").value.trim();

  if (!/^[A-Za-z0-9._-]{1,64}$/.test(name)) {
    result.textContent = "scenario name must be 1-64 chars: letters, numbers, dot, underscore, hyphen only";
    result.className = "result error";
    return;
  }

  let schedule;
  try {
    schedule = state.steps.map(buildStepPayload);
  } catch (err) {
    result.textContent = err.message;
    result.className = "result error";
    return;
  }

  const body = { name, persona, schedule };
  const submitBtn = e.target.querySelector("button[type=submit]");
  submitBtn.disabled = true;
  try {
    await postScenario(body, false);
    result.innerHTML = `saved "${name}". <a href="./">View it on the dashboard &rarr;</a>`;
    result.className = "result success";
  } catch (err) {
    if (err.status === 409) {
      if (confirm(`${err.message}\n\nOverwrite it?`)) {
        try {
          await postScenario(body, true);
          result.innerHTML = `saved "${name}" (overwritten). <a href="./">View it on the dashboard &rarr;</a>`;
          result.className = "result success";
        } catch (err2) {
          result.textContent = `couldn't save: ${err2.message}`;
          result.className = "result error";
        }
      } else {
        result.textContent = "not saved — choose a different name or confirm overwrite.";
        result.className = "result error";
      }
    } else {
      result.textContent = `couldn't save: ${err.message}`;
      result.className = "result error";
    }
  } finally {
    submitBtn.disabled = false;
  }
}

document.getElementById("steps-root").addEventListener("input", (e) => {
  const field = e.target.dataset.field;
  if (!field) return;
  const i = Number(e.target.dataset.index);
  const step = state.steps[i];
  if (e.target.type === "checkbox") step[field] = e.target.checked;
  else if (["delayLo", "delayHi", "durationLo", "durationHi"].includes(field)) {
    step[field] = e.target.value === "" ? 0 : Number(e.target.value);
  } else step[field] = e.target.value;

  if (field === "action" || field === "hasDuration") renderSteps();
  renderPreview();
});

document.getElementById("steps-root").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-remove]");
  if (!btn) return;
  state.steps.splice(Number(btn.dataset.remove), 1);
  renderSteps();
  renderPreview();
});

document.getElementById("add-step-btn").addEventListener("click", () => {
  state.steps.push(defaultStep());
  renderSteps();
  renderPreview();
});

document.getElementById("persona-input").addEventListener("input", renderPreview);
document.getElementById("scenario-form").addEventListener("submit", handleSubmit);

renderSteps();
renderPreview();

(async function gateForRole() {
  const user = await setupWhoami();
  if (user && user.role !== "admin") {
    document.getElementById("builder-card").style.display = "none";
    document.getElementById("viewer-blocked-card").style.display = "";
  }
})();
