// Login page: POSTs to /auth/login, which sets the session cookie on
// success, then redirects back to wherever the 401 redirect came from
// (see auth.js's authedFetch -- it appends ?next=<path>).

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = document.getElementById("login-result");
  const username = document.getElementById("username-input").value;
  const password = document.getElementById("password-input").value;

  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || res.statusText);
    }
    const params = new URLSearchParams(window.location.search);
    window.location.href = params.get("next") || "./";
  } catch (err) {
    result.textContent = `login failed: ${err.message}`;
    result.className = "result error";
  }
});
