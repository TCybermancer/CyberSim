// Live topology view: puppet hosts on the left, target infrastructure on
// the right, connected by an animated link only while a host has a real
// action in flight (GET /agents/live-status). Link color encodes
// should_alert -- the point is to make red-team activity visible at a
// glance, the same way a detection tool should see it. office_doc has no
// network target (it's local file/process activity), so it shows as a
// pulsing ring on the host node instead of a line.

const SVG_NS = "http://www.w3.org/2000/svg";

const TARGETS = [
  { key: "web_browse", label: "Web" },
  { key: "email_send", label: "Mail" },
  { key: "smb_access", label: "File Share" },
];

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function renderTopology(agents) {
  const root = document.getElementById("topology-root");
  const countLabel = document.getElementById("topology-count");
  const activeCount = agents.filter((a) => a.current_action).length;

  countLabel.textContent = agents.length
    ? `${agents.length} host${agents.length === 1 ? "" : "s"} · ${activeCount} active`
    : "";

  if (!agents.length) {
    root.innerHTML =
      '<p class="hint topology-empty">No hosts have checked in yet. ' +
      '<a href="install.html">Install an agent</a> to see live activity here.</p>';
    return;
  }

  const reduced = prefersReducedMotion();
  const rowHeight = 52;
  const topPad = 36;
  const width = 640;
  const height = Math.max(200, topPad * 2 + rowHeight * (agents.length - 1));
  const hostX = 88;
  const targetX = width - 88;

  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    class: "topology-svg",
    role: "img",
    "aria-label": `Live topology: ${agents.length} host${agents.length === 1 ? "" : "s"}, ${activeCount} currently active`,
  });

  const defs = svgEl("defs");
  const pattern = svgEl("pattern", {
    id: "topo-grid",
    width: 22,
    height: 22,
    patternUnits: "userSpaceOnUse",
  });
  pattern.appendChild(svgEl("circle", { cx: 1, cy: 1, r: 1, class: "topo-grid-dot" }));
  defs.appendChild(pattern);
  svg.appendChild(defs);
  svg.appendChild(svgEl("rect", { width, height, fill: "url(#topo-grid)" }));

  // Target nodes, evenly spaced, fed (highlighted) if anything currently
  // points at them.
  const fedTargets = new Set(
    agents.map((a) => a.current_action?.action_type).filter((t) => t)
  );
  const targetY = {};
  TARGETS.forEach((t, i) => {
    const y = (height / (TARGETS.length + 1)) * (i + 1);
    targetY[t.key] = y;
    const g = svgEl("g");
    g.appendChild(
      svgEl("circle", {
        cx: targetX,
        cy: y,
        r: 8,
        class: `topo-target-node${fedTargets.has(t.key) ? " is-fed" : ""}`,
      })
    );
    const label = svgEl("text", { x: targetX + 15, y: y + 4, class: "topo-label" });
    label.textContent = t.label;
    g.appendChild(label);
    svg.appendChild(g);
  });

  // Hosts + their links.
  agents.forEach((agent, i) => {
    const y = topPad + i * rowHeight;
    const action = agent.current_action;
    const alert = Boolean(action?.should_alert);
    const targetKey = action && targetY[action.action_type] !== undefined ? action.action_type : null;
    const isLocal = Boolean(action) && !targetKey; // office_doc: local, no network target

    if (targetKey) {
      const ty = targetY[targetKey];
      const midX = (hostX + targetX) / 2;
      const pathId = `topo-path-${i}`;
      const path = svgEl("path", {
        id: pathId,
        d: `M ${hostX} ${y} C ${midX} ${y}, ${midX} ${ty}, ${targetX} ${ty}`,
        class: `topo-link ${alert ? "topo-link-alert" : "topo-link-benign"}${reduced ? "" : " is-flowing"}`,
      });
      svg.appendChild(path);

      if (alert && !reduced) {
        const packet = svgEl("circle", { r: 3.5, class: "topo-packet" });
        const anim = svgEl("animateMotion", { dur: "1s", repeatCount: "indefinite" });
        const mpath = svgEl("mpath");
        mpath.setAttribute("href", `#${pathId}`);
        anim.appendChild(mpath);
        packet.appendChild(anim);
        svg.appendChild(packet);
      }
    }

    const group = svgEl("g");

    if (isLocal) {
      group.appendChild(
        svgEl("circle", {
          cx: hostX,
          cy: y,
          r: 13,
          class: `topo-ring${alert ? " is-alert" : ""}${reduced ? "" : " is-pulsing"}`,
        })
      );
    }

    const nodeClass = !action ? "topo-node-idle" : alert ? "topo-node-alert" : "topo-node-active";
    group.appendChild(svgEl("circle", { cx: hostX, cy: y, r: 6, class: `topo-node ${nodeClass}` }));

    const hostLabel = svgEl("text", {
      x: hostX - 20,
      y: y + 4,
      "text-anchor": "end",
      class: "topo-label topo-label-host",
    });
    hostLabel.textContent = agent.host;
    group.appendChild(hostLabel);

    if (action) {
      const actionLabel = svgEl("text", {
        x: hostX,
        y: y - 16,
        "text-anchor": "middle",
        class: `topo-label topo-action-label${alert ? " is-alert" : ""}`,
      });
      actionLabel.textContent = action.action_type;
      group.appendChild(actionLabel);

      if (alert) {
        group.appendChild(
          iconSvgEl("alert", {
            transform: `translate(${hostX + 7}, ${y - 14}) scale(0.55)`,
            class: "topo-alert-badge",
          })
        );
      }
    }

    svg.appendChild(group);
  });

  root.innerHTML = "";
  root.appendChild(svg);
}
