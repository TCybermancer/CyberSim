// Schedule (gantt) view for a run's ledger: one lane per host, each action
// drawn as a bar spanning its intended start -> planned end. Color encodes
// status (pending/in progress/success/failure), same vocabulary as the
// ledger table below it. should_alert actions get a dashed red outline
// rather than relying on color alone, echoing the topology view's
// convention for red-team activity. Clicking a bar scrolls to and flashes
// its row in the ledger table, so the two views stay connected.

const GANTT_SVG_NS = "http://www.w3.org/2000/svg";

function ganttEl(tag, attrs = {}) {
  const el = document.createElementNS(GANTT_SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function ganttStatus(entry) {
  if (entry.completion) return entry.completion.exit_status; // success | failure | partial
  if (entry.intent) return "in progress";
  return "pending";
}

function ganttPrefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function fmtClock(date) {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function highlightLedgerRow(actionId) {
  const row = document.querySelector(`#ledger-table tr[data-action-id="${actionId}"]`);
  if (!row) return;
  row.scrollIntoView({ block: "nearest", behavior: ganttPrefersReducedMotion() ? "auto" : "smooth" });
  row.classList.remove("flash");
  // eslint-disable-next-line no-unused-expressions -- force reflow so the flash animation restarts on repeat clicks
  void row.offsetWidth;
  row.classList.add("flash");
}

function renderGantt(ledger) {
  const root = document.getElementById("gantt-root");
  const entries = Object.values(ledger).map((e) => {
    const start = new Date(e.spec.intended_start);
    const durationSec = Math.max(e.spec.params?.duration_seconds || 0, 0);
    let end = new Date(start.getTime() + durationSec * 1000);
    if (e.completion) {
      const actualEnd = new Date(e.completion.actual_end);
      if (actualEnd > end) end = actualEnd;
    }
    return { ...e, start, end };
  });

  if (!entries.length) {
    root.innerHTML = "";
    return;
  }

  const hosts = [...new Set(entries.map((e) => e.spec.host))].sort();

  let minT = Math.min(...entries.map((e) => e.start.getTime()));
  let maxT = Math.max(...entries.map((e) => e.end.getTime()));
  if (minT === maxT) {
    minT -= 30000;
    maxT += 30000;
  } else {
    const pad = (maxT - minT) * 0.04;
    minT -= pad;
    maxT += pad;
  }

  const reduced = ganttPrefersReducedMotion();
  const rowHeight = 34;
  const topPad = 28;
  const leftPad = 118;
  const rightPad = 16;
  const width = 640;
  const height = topPad + rowHeight * hosts.length + 8;
  const plotW = width - leftPad - rightPad;
  const scaleX = (t) => leftPad + ((t - minT) / (maxT - minT)) * plotW;

  const svg = ganttEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    class: "gantt-svg",
    role: "img",
    "aria-label": `Run schedule: ${entries.length} action${entries.length === 1 ? "" : "s"} across ${hosts.length} host${hosts.length === 1 ? "" : "s"}`,
  });

  // "partial" gets a hatch fill so it reads distinctly from "failure" even
  // though both use the --bad color -- two outcomes shouldn't look identical.
  const defs = ganttEl("defs");
  const stripes = ganttEl("pattern", {
    id: "gantt-partial-stripes",
    width: 6,
    height: 6,
    patternUnits: "userSpaceOnUse",
    patternTransform: "rotate(45)",
  });
  stripes.appendChild(ganttEl("rect", { width: 6, height: 6, class: "gantt-stripe-bg" }));
  stripes.appendChild(ganttEl("line", { x1: 0, y1: 0, x2: 0, y2: 6, class: "gantt-stripe-line" }));
  defs.appendChild(stripes);
  svg.appendChild(defs);

  const tickCount = 5;
  for (let i = 0; i <= tickCount; i++) {
    const t = minT + ((maxT - minT) / tickCount) * i;
    const x = scaleX(t);
    svg.appendChild(
      ganttEl("line", { x1: x, x2: x, y1: topPad - 6, y2: height - 2, class: "gantt-gridline" })
    );
    const label = ganttEl("text", { x, y: topPad - 12, "text-anchor": "middle", class: "gantt-tick-label" });
    label.textContent = fmtClock(new Date(t));
    svg.appendChild(label);
  }

  const hostIndex = {};
  hosts.forEach((host, i) => {
    hostIndex[host] = i;
    const y = topPad + rowHeight * i + rowHeight / 2;
    svg.appendChild(
      ganttEl("line", { x1: leftPad, x2: width - rightPad, y1: y, y2: y, class: "gantt-lane" })
    );
    const label = ganttEl("text", {
      x: leftPad - 12,
      y: y + 4,
      "text-anchor": "end",
      class: "gantt-label gantt-label-host",
    });
    label.textContent = host;
    svg.appendChild(label);
  });

  entries.forEach((e) => {
    const y = topPad + rowHeight * hostIndex[e.spec.host] + rowHeight / 2;
    const x1 = scaleX(e.start.getTime());
    const x2 = scaleX(e.end.getTime());
    const barW = Math.max(x2 - x1, 5);
    const status = ganttStatus(e);
    const alert = e.spec.should_alert;
    const statusSlug = status.replace(" ", "-");

    const bar = ganttEl("rect", {
      x: x1,
      y: y - 8,
      width: barW,
      height: 16,
      rx: 4,
      class: `gantt-bar gantt-bar-${statusSlug}${alert ? " is-alert" : ""}${
        status === "in progress" && !reduced ? " is-pulsing" : ""
      }`,
      tabindex: "0",
      role: "button",
      "aria-label": `${e.spec.action_type}${alert ? ", flagged red team" : ""}, ${status}, ${fmtClock(e.start)} to ${fmtClock(e.end)} on ${e.spec.host}`,
    });
    const title = ganttEl("title");
    title.textContent = `${e.spec.action_type}${alert ? " [RED TEAM]" : ""} — ${status} — ${fmtClock(e.start)}–${fmtClock(e.end)}`;
    bar.appendChild(title);

    const activate = () => highlightLedgerRow(e.spec.action_id);
    bar.addEventListener("click", activate);
    bar.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter" || evt.key === " ") {
        evt.preventDefault();
        activate();
      }
    });

    svg.appendChild(bar);
  });

  root.innerHTML = "";
  root.appendChild(svg);
}
