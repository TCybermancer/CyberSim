// Shared auth helpers for every dashboard page: wraps fetch so a 401
// (missing/expired session) redirects to the login page instead of
// leaving the caller to render a broken, half-authenticated UI, and
// wires up the "log out" pill present in each page's header.

async function authedFetch(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401 && !window.location.pathname.endsWith("/login.html")) {
    const next = window.location.pathname + window.location.search;
    window.location.href = `login.html?next=${encodeURIComponent(next)}`;
    throw new Error("not authenticated");
  }
  return res;
}

function setupLogout() {
  const btn = document.getElementById("logout-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "login.html";
  });
}

let _currentUserPromise = null;

// Cached for the life of the page -- every page that cares fetches this
// once (e.g. to show "username (role)" in the header, or to hide/disable
// mutating controls for viewer accounts) rather than each independently
// hitting /auth/me.
function getCurrentUser() {
  if (!_currentUserPromise) {
    _currentUserPromise = authedFetch("/auth/me").then((res) => res.json());
  }
  return _currentUserPromise;
}

const NAV_LINK_IDS = ["nav-scenario-builder", "nav-install", "nav-users", "nav-settings"];

// Every admin-only page (build scenario, install, users, settings) is
// linked from the sidebar on *every* page, not just the dashboard --
// centralized here so hiding those links for viewer accounts is one
// implementation instead of one per page.
function applyNavRoleGating(role) {
  const isAdmin = role === "admin";
  NAV_LINK_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.style.display = isAdmin ? "" : "none";
  });
}

async function setupWhoami() {
  let user;
  try {
    user = await getCurrentUser();
  } catch {
    return null; // authedFetch already redirected to login on 401
  }
  applyNavRoleGating(user.role);
  const pill = document.getElementById("whoami-pill");
  if (pill) pill.textContent = `${user.username} (${user.role})`;
  return user;
}
