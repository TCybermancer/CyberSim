// Install page: Windows/Linux tabs, each with its own download form. Shows
// this server's own address (so it's obvious what the download will
// auto-link to) and offers known-but-unregistered hostnames as a hint for
// the Host ID fields, via a <datalist> shared by both.

document.querySelectorAll(".server-url-display").forEach((el) => {
  el.textContent = window.location.origin;
});

function selectOsTab(name) {
  const tabs = { windows: "tab-windows", linux: "tab-linux" };
  const buttons = { windows: "tab-btn-windows", linux: "tab-btn-linux" };
  for (const key of Object.keys(tabs)) {
    const active = key === name;
    document.getElementById(tabs[key]).style.display = active ? "" : "none";
    const btn = document.getElementById(buttons[key]);
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
}

document.getElementById("tab-btn-windows").addEventListener("click", () => selectOsTab("windows"));
document.getElementById("tab-btn-linux").addEventListener("click", () => selectOsTab("linux"));

(async function gateForRole() {
  const user = await setupWhoami();
  if (user && user.role !== "admin") {
    document.getElementById("download-card").style.display = "none";
    document.getElementById("viewer-blocked-card").style.display = "";
  }
})();

(async function suggestHostIds() {
  try {
    const res = await authedFetch("/agents");
    if (!res.ok) return;
    const { agents } = await res.json();
    if (!agents.length) return;

    const datalist = document.createElement("datalist");
    datalist.id = "known-hosts-datalist";
    datalist.innerHTML = agents.map((a) => `<option value="${a.host}">`).join("");
    document.body.appendChild(datalist);
    document.querySelectorAll(".host-id-input").forEach((input) => {
      input.setAttribute("list", "known-hosts-datalist");
    });
  } catch {
    /* best-effort suggestion only -- the form works fine without it */
  }
})();
