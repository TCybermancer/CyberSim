// Install page: shows this server's own address (so it's obvious what
// the download will auto-link to) and offers known-but-unregistered
// hostnames as a hint for the Host ID field, via a <datalist>.

document.getElementById("server-url-display").textContent = window.location.origin;

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
    document.getElementById("host-id-input").setAttribute("list", "known-hosts-datalist");
  } catch {
    /* best-effort suggestion only -- the form works fine without it */
  }
})();
