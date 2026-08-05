// Dashboard account management (admin only). Self-contained small
// api() helper rather than sharing app.js's, since this page doesn't
// load the rest of the dashboard's scripts.

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

function fmtTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

let selfUsername = null;

async function loadUsers() {
  const { users } = await api("/users");
  const tbody = document.querySelector("#users-table tbody");
  tbody.innerHTML = users
    .map((u) => {
      const isSelf = u.username === selfUsername;
      return `<tr>
        <td>${u.username}${isSelf ? ' <span class="hint">(you)</span>' : ""}</td>
        <td>${u.role}</td>
        <td>${fmtTime(u.created_at)}</td>
        <td>${
          isSelf
            ? ""
            : `<button type="button" class="chip" data-delete-user="${u.username}">${iconHtml(
                "failure"
              )} remove</button>`
        }</td>
      </tr>`;
    })
    .join("");
}

document.querySelector("#users-table tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-delete-user]");
  if (!btn) return;
  const username = btn.dataset.deleteUser;
  if (!confirm(`Remove account "${username}"? This can't be undone.`)) return;
  btn.disabled = true;
  try {
    await api(`/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    await loadUsers();
  } catch (err) {
    alert(`couldn't remove "${username}": ${err.message}`);
    btn.disabled = false;
  }
});

document.getElementById("create-user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = document.getElementById("create-user-result");
  const submitBtn = e.target.querySelector("button[type=submit]");

  const payload = {
    username: document.getElementById("new-username-input").value.trim(),
    password: document.getElementById("new-password-input").value,
    role: document.getElementById("new-role-input").value,
  };

  submitBtn.disabled = true;
  try {
    await api("/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    result.textContent = `created "${payload.username}" (${payload.role})`;
    result.className = "result success";
    e.target.reset();
    await loadUsers();
  } catch (err) {
    result.textContent = `couldn't create account: ${err.message}`;
    result.className = "result error";
  } finally {
    submitBtn.disabled = false;
  }
});

(async function init() {
  const user = await setupWhoami();
  if (!user) return;
  if (user.role !== "admin") {
    document.getElementById("users-card").style.display = "none";
    document.getElementById("add-user-card").style.display = "none";
    document.getElementById("viewer-blocked-card").style.display = "";
    return;
  }
  selfUsername = user.username;
  await loadUsers();
})();
