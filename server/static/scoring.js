// Scoring view: uploads a detection-tool alert export and renders the
// precision/recall/detection-latency report from POST /runs/{id}/score
// (server-side scoring_core.py -- the same matching logic scoring/cli.py
// uses, so the dashboard and the CLI always agree on a run's score).

function fmtPct(x) {
  return x === null || x === undefined ? "n/a" : `${Math.round(x * 1000) / 10}%`;
}

function scoreStatHtml(value, label) {
  return `<div class="score-stat"><span class="score-stat-value">${value}</span><span class="score-stat-label">${label}</span></div>`;
}

function renderOverallStats(overall) {
  return (
    `<div class="score-stats">` +
    scoreStatHtml(fmtPct(overall.precision_including_unattributed), "precision") +
    scoreStatHtml(fmtPct(overall.recall), "recall") +
    scoreStatHtml(fmtPct(overall.f1), "f1") +
    scoreStatHtml(overall.true_positives, "caught") +
    scoreStatHtml(overall.false_negatives, "missed") +
    scoreStatHtml(overall.false_positives_total, "false positives") +
    `</div>`
  );
}

function renderGroupTable(title, groups) {
  const rows = Object.entries(groups)
    .map(
      ([name, g]) =>
        `<tr><td>${name}</td><td>${g.should_alert_total}</td><td>${g.true_positives}</td><td>${
          g.false_negatives
        }</td><td>${g.false_positives_benign_flagged}</td><td>${fmtPct(g.precision)}</td><td>${fmtPct(
          g.recall
        )}</td></tr>`
    )
    .join("");
  if (!rows) return "";
  return `<h3 class="score-subhead">${title}</h3>
    <div class="table-scroll">
      <table class="score-group-table">
        <thead><tr><th></th><th>red team</th><th>caught</th><th>missed</th><th>FP</th><th>precision</th><th>recall</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderFalseNegatives(list) {
  if (!list.length) return "";
  const items = list
    .map(
      (fn) =>
        `<li>${iconHtml("alert", "icon-alert-inline")} <strong>${fn.action_type}</strong> on ${
          fn.host
        } &mdash; intended ${new Date(fn.intended_start).toLocaleString()}${
          fn.executed ? "" : " (never executed)"
        }</li>`
    )
    .join("");
  return `<h3 class="score-subhead">Missed detections</h3><ul class="score-list">${items}</ul>`;
}

function renderFalsePositives(list) {
  if (!list.length) return "";
  const items = list
    .map((fp) =>
      fp.action_id
        ? `<li>${iconHtml("failure")} <strong>${fp.action_type}</strong> on ${
            fp.host
          } &mdash; benign, but matched rule(s): ${fp.matched_alert_rules.join(", ")}</li>`
        : `<li>${iconHtml("failure")} <em>unattributed</em> on ${fp.host} &mdash; rule "${
            fp.alert_rule
          }" at ${new Date(fp.alert_timestamp).toLocaleString()}, no matching action</li>`
    )
    .join("");
  return `<h3 class="score-subhead">False positives</h3><ul class="score-list">${items}</ul>`;
}

function renderScoreReport(scores) {
  const root = document.getElementById("scoring-report");
  root.innerHTML =
    renderOverallStats(scores.overall) +
    renderGroupTable("By action type", scores.by_action_type) +
    renderGroupTable("By persona", scores.by_persona) +
    renderFalseNegatives(scores.false_negatives) +
    renderFalsePositives(scores.false_positives);
}

function setupScoringForm() {
  document.getElementById("scoring-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const result = document.getElementById("scoring-result");
    const reportRoot = document.getElementById("scoring-report");
    const submitBtn = document.getElementById("scoring-submit-btn");

    if (!selectedRunId) {
      result.textContent = "select a run above first";
      result.className = "result error";
      return;
    }

    const file = document.getElementById("alerts-file-input").files[0];
    if (!file) {
      result.textContent = "choose an alert export file";
      result.className = "result error";
      return;
    }

    const formData = new FormData();
    formData.append("alerts_file", file);
    const windowBefore = document.getElementById("window-before-input").value;
    const windowAfter = document.getElementById("window-after-input").value;
    if (windowBefore !== "") formData.append("window_before", windowBefore);
    if (windowAfter !== "") formData.append("window_after", windowAfter);

    submitBtn.disabled = true;
    reportRoot.innerHTML = "";
    result.textContent = "";
    result.className = "result";
    try {
      const scores = await api(`/runs/${selectedRunId}/score`, { method: "POST", body: formData });
      renderScoreReport(scores);
      result.textContent = `scored against ${file.name}`;
      result.className = "result success";
    } catch (err) {
      result.textContent = `scoring failed: ${err.message}`;
      result.className = "result error";
    } finally {
      submitBtn.disabled = false;
    }
  });
}

setupScoringForm();
