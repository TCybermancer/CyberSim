// Settings page: "General" (network/LLM config, admin only) and
// "Security" (self-service password change, every logged-in role) tabs.
// Self-contained small api() helper, same as users.js, since this page
// doesn't load app.js.

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

// ---- tabs -------------------------------------------------------------

function selectTab(name) {
  const tabs = { general: "tab-general", security: "tab-security", "remote-install": "tab-remote-install" };
  const buttons = { general: "tab-btn-general", security: "tab-btn-security", "remote-install": "tab-btn-remote-install" };
  for (const key of Object.keys(tabs)) {
    const active = key === name;
    document.getElementById(tabs[key]).style.display = active ? "" : "none";
    const btn = document.getElementById(buttons[key]);
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
}

document.getElementById("tab-btn-general").addEventListener("click", () => selectTab("general"));
document.getElementById("tab-btn-security").addEventListener("click", () => selectTab("security"));
document.getElementById("tab-btn-remote-install").addEventListener("click", () => selectTab("remote-install"));

// ---- change password (any logged-in role) ------------------------------

document.getElementById("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = document.getElementById("password-result");
  const submitBtn = e.target.querySelector("button[type=submit]");

  const current_password = document.getElementById("current-password-input").value;
  const new_password = document.getElementById("new-password-input").value;
  const confirm_password = document.getElementById("confirm-password-input").value;

  if (new_password !== confirm_password) {
    result.textContent = "new password and confirmation don't match";
    result.className = "result error";
    return;
  }

  submitBtn.disabled = true;
  try {
    await api("/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password, new_password }),
    });
    e.target.reset();
    result.textContent = "password changed";
    result.className = "result success";
  } catch (err) {
    result.textContent = `couldn't change password: ${err.message}`;
    result.className = "result error";
  } finally {
    submitBtn.disabled = false;
  }
});

function showProviderFields(provider) {
  document.querySelectorAll(".provider-fields").forEach((el) => (el.style.display = "none"));
  const active = document.getElementById(`${provider}-fields`);
  if (active) active.style.display = "";
}

function applySettings(s) {
  document.getElementById("connected-toggle").checked = s.network_mode === "connected";
  document.getElementById("provider-fields").style.display = s.network_mode === "connected" ? "" : "none";
  document.getElementById("provider-select").value = s.llm_provider;
  showProviderFields(s.llm_provider);

  document.getElementById("anthropic-key-status").textContent = s.anthropic_key_set ? "(configured — leave blank to keep it)" : "(not set)";
  document.getElementById("anthropic-model-input").value = s.anthropic_model || "";
  document.getElementById("openai-key-status").textContent = s.openai_key_set ? "(configured — leave blank to keep it)" : "(not set)";
  document.getElementById("openai-model-input").value = s.openai_model || "";
  document.getElementById("local-key-status").textContent = s.local_key_set ? "(configured — leave blank to keep it)" : "(not set)";
  document.getElementById("local-url-input").value = s.local_base_url || "";
  document.getElementById("local-model-input").value = s.local_model || "";

  document.getElementById("remote-linux-user-input").value = s.remote_linux_ssh_user || "";
  document.getElementById("remote-linux-key-status").textContent = s.remote_linux_ssh_key_set ? "(configured — leave blank to keep it)" : "(not set)";
  document.getElementById("remote-linux-password-status").textContent = s.remote_linux_ssh_password_set ? "(configured — leave blank to keep it)" : "(not set)";
  document.getElementById("remote-windows-user-input").value = s.remote_windows_winrm_user || "";
  document.getElementById("remote-windows-password-status").textContent = s.remote_windows_winrm_password_set ? "(configured — leave blank to keep it)" : "(not set)";
  document.getElementById("remote-server-url-input").value = s.remote_install_server_url || "";

  document.getElementById("mail-server-host-input").value = s.mail_server_host || "";
  document.getElementById("mail-server-port-input").value = s.mail_server_port || "";
}

document.getElementById("connected-toggle").addEventListener("change", (e) => {
  document.getElementById("provider-fields").style.display = e.target.checked ? "" : "none";
});

document.getElementById("provider-select").addEventListener("change", (e) => {
  showProviderFields(e.target.value);
});

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = document.getElementById("settings-result");
  const submitBtn = e.target.querySelector("button[type=submit]");

  const payload = {
    network_mode: document.getElementById("connected-toggle").checked ? "connected" : "airgapped",
    llm_provider: document.getElementById("provider-select").value,
    anthropic_model: document.getElementById("anthropic-model-input").value,
    openai_model: document.getElementById("openai-model-input").value,
    local_base_url: document.getElementById("local-url-input").value,
    local_model: document.getElementById("local-model-input").value,
    mail_server_host: document.getElementById("mail-server-host-input").value,
  };
  // Port is an int-or-null field server-side -- an empty string isn't a
  // valid int, so (unlike the plain-string fields above) this has to be
  // omitted rather than sent blank, or a blank port would 422 even when
  // nothing about it actually changed.
  const mailServerPort = document.getElementById("mail-server-port-input").value;
  if (mailServerPort) payload.mail_server_port = parseInt(mailServerPort, 10);
  // API key fields: only send if the admin actually typed something --
  // an empty masked field means "leave it alone", not "clear it". The
  // server's PUT only touches keys present in the body (exclude_unset),
  // so simply omitting these when blank is what makes that work.
  const anthropicKey = document.getElementById("anthropic-key-input").value;
  if (anthropicKey) payload.anthropic_api_key = anthropicKey;
  const openaiKey = document.getElementById("openai-key-input").value;
  if (openaiKey) payload.openai_api_key = openaiKey;
  const localKey = document.getElementById("local-key-input").value;
  if (localKey) payload.local_api_key = localKey;

  submitBtn.disabled = true;
  try {
    const updated = await api("/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applySettings(updated);
    document.getElementById("anthropic-key-input").value = "";
    document.getElementById("openai-key-input").value = "";
    document.getElementById("local-key-input").value = "";
    result.textContent = "settings saved";
    result.className = "result success";
  } catch (err) {
    result.textContent = `couldn't save settings: ${err.message}`;
    result.className = "result error";
  } finally {
    submitBtn.disabled = false;
  }
});

document.getElementById("remote-install-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = document.getElementById("remote-install-settings-result");
  const submitBtn = e.target.querySelector("button[type=submit]");

  const payload = {
    remote_linux_ssh_user: document.getElementById("remote-linux-user-input").value,
    remote_windows_winrm_user: document.getElementById("remote-windows-user-input").value,
    remote_install_server_url: document.getElementById("remote-server-url-input").value,
  };
  // Same "blank means leave it alone" convention as the LLM API keys above.
  const sshKey = document.getElementById("remote-linux-key-input").value;
  if (sshKey) payload.remote_linux_ssh_private_key = sshKey;
  const sshPassword = document.getElementById("remote-linux-password-input").value;
  if (sshPassword) payload.remote_linux_ssh_password = sshPassword;
  const winrmPassword = document.getElementById("remote-windows-password-input").value;
  if (winrmPassword) payload.remote_windows_winrm_password = winrmPassword;

  submitBtn.disabled = true;
  try {
    const updated = await api("/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applySettings(updated);
    document.getElementById("remote-linux-key-input").value = "";
    document.getElementById("remote-linux-password-input").value = "";
    document.getElementById("remote-windows-password-input").value = "";
    result.textContent = "remote install credentials saved";
    result.className = "result success";
  } catch (err) {
    result.textContent = `couldn't save remote install credentials: ${err.message}`;
    result.className = "result error";
  } finally {
    submitBtn.disabled = false;
  }
});

(async function init() {
  const user = await setupWhoami();
  if (!user) return;
  if (user.role !== "admin") {
    document.getElementById("settings-card").style.display = "none";
    document.getElementById("viewer-blocked-card").style.display = "";
    // Non-admins can't touch General or Remote Install at all -- land
    // them on the one tab that's actually theirs (changing their own
    // password) instead of a dead end.
    document.getElementById("tab-btn-general").style.display = "none";
    document.getElementById("tab-btn-remote-install").style.display = "none";
    selectTab("security");
    return;
  }
  try {
    const settings = await api("/settings");
    applySettings(settings);
  } catch (err) {
    document.getElementById("settings-result").textContent = `couldn't load settings: ${err.message}`;
    document.getElementById("settings-result").className = "result error";
  }
})();
