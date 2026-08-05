// Install page: Windows/Linux/Remote Install tabs. Windows and Linux each
// have their own download form; Remote Install triggers an install over
// SSH/WinRM (see Settings -> Remote Install for the credentials it uses).
// Shows this server's own address (so it's obvious what the download will
// auto-link to) and offers known-but-unregistered hostnames as a hint for
// the Host ID fields, via a <datalist> shared by all three.

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

document.querySelectorAll(".server-url-display").forEach((el) => {
  el.textContent = window.location.origin;
});

function selectOsTab(name) {
  const tabs = { windows: "tab-windows", linux: "tab-linux", "remote-install": "tab-remote-install" };
  const buttons = { windows: "tab-btn-windows", linux: "tab-btn-linux", "remote-install": "tab-btn-remote-install" };
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
document.getElementById("tab-btn-remote-install").addEventListener("click", () => selectOsTab("remote-install"));

document.getElementById("remote-install-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = document.getElementById("remote-install-result");
  const output = document.getElementById("remote-install-output");
  const submitBtn = document.getElementById("remote-install-submit-btn");

  const payload = {
    ip: document.getElementById("remote-ip-input").value,
    os: document.getElementById("remote-os-select").value,
    host_id: document.getElementById("remote-host-id-input").value,
    persona: document.getElementById("remote-persona-input").value || "default",
  };

  output.style.display = "none";
  output.textContent = "";
  submitBtn.disabled = true;
  result.textContent = "installing… this can take a minute";
  result.className = "result";
  try {
    const body = await api("/install/remote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (body.status === "ok") {
      result.textContent = `install succeeded on ${payload.ip}`;
      result.className = "result success";
    } else {
      result.textContent = `install ran but failed on ${payload.ip} (exit code ${body.exit_code})`;
      result.className = "result error";
    }
    const combined = [body.stdout, body.stderr].filter(Boolean).join("\n---\n");
    if (combined) {
      output.textContent = combined;
      output.style.display = "";
    }
  } catch (err) {
    result.textContent = `couldn't install: ${err.message}`;
    result.className = "result error";
  } finally {
    submitBtn.disabled = false;
  }
});

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
