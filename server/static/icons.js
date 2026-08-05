// Shared status/alert icon system. Small stroke-based line glyphs (same
// visual family as the topology/gantt views' thin line art) defined once
// as raw SVG markup, so the HTML-string contexts (table cells, the health
// pill) and the SVG-DOM contexts (topology.js, gantt.js) render the exact
// same glyph instead of drifting into inconsistent iconography or emoji.

const ICONS = {
  success:
    '<path d="M3.5 8.5 L6.5 11.5 L12.5 4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  failure:
    '<path d="M4 4 L12 12 M12 4 L4 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  partial:
    '<circle cx="8" cy="8" r="5.4" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M5 8 H11" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  pending:
    '<circle cx="8" cy="8" r="5.4" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M8 5 V8 L10.1 9.3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
  progress:
    '<circle class="icon-spin" cx="8" cy="8" r="5.4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-dasharray="17 26"/>',
  alert:
    '<path d="M8 2.2 L14.6 13.6 H1.4 Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>' +
    '<path d="M8 6.6 V9.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
    '<circle cx="8" cy="11.6" r="0.9" fill="currentColor" stroke="none"/>',
};

const STATUS_ICON_KEY = {
  success: "success",
  failure: "failure",
  partial: "partial",
  "in progress": "progress",
  pending: "pending",
};

function iconHtml(name, extraClass = "") {
  const inner = ICONS[name];
  if (!inner) return "";
  return `<svg class="icon ${extraClass}" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">${inner}</svg>`;
}

function statusIconHtml(status, extraClass = "") {
  return iconHtml(STATUS_ICON_KEY[status] || "pending", extraClass);
}

// SVG-DOM variant for contexts that build the tree directly (topology.js,
// gantt.js) instead of via innerHTML strings.
function iconSvgEl(name, attrs = {}) {
  const NS = "http://www.w3.org/2000/svg";
  const g = document.createElementNS(NS, "g");
  g.innerHTML = ICONS[name] || "";
  g.setAttribute("aria-hidden", "true"); // decorative -- the parent SVG's own aria-label carries the meaning
  for (const [k, v] of Object.entries(attrs)) g.setAttribute(k, v);
  return g;
}
