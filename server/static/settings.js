// Network/LLM settings page (admin only). Self-contained small api()
// helper, same as users.js, since this page doesn't load app.js.

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
  };
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

(async function init() {
  const user = await setupWhoami();
  if (!user) return;
  if (user.role !== "admin") {
    document.getElementById("settings-card").style.display = "none";
    document.getElementById("viewer-blocked-card").style.display = "";
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
