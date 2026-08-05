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

async function setupWhoami() {
  const pill = document.getElementById("whoami-pill");
  if (!pill) return null;
  try {
    const user = await getCurrentUser();
    pill.textContent = `${user.username} (${user.role})`;
    return user;
  } catch {
    return null; // authedFetch already redirected to login on 401
  }
}
