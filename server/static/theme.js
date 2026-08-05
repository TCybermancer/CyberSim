// Theme toggle: cycles auto (follow OS) -> light -> dark -> auto,
// persisted in localStorage. Avoiding a flash of the wrong theme on load
// happens separately, via a tiny inline <script> in each page's <head>
// that applies any stored override before CSS paints -- this file only
// wires up the toggle control's click behavior once the DOM is ready.

const THEME_KEY = "cybersim-theme";
const THEME_CYCLE = [null, "light", "dark"]; // null = auto

function currentTheme() {
  return localStorage.getItem(THEME_KEY);
}

function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
}

function themeLabel(theme) {
  if (theme === "light") return "light";
  if (theme === "dark") return "dark";
  return "auto";
}

function setupThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;

  const render = () => {
    const label = themeLabel(currentTheme());
    btn.textContent = `theme: ${label}`;
    btn.setAttribute("aria-label", `Theme is ${label}. Activate to switch to the next theme.`);
  };

  btn.addEventListener("click", () => {
    const next = THEME_CYCLE[(THEME_CYCLE.indexOf(currentTheme()) + 1) % THEME_CYCLE.length];
    if (next) localStorage.setItem(THEME_KEY, next);
    else localStorage.removeItem(THEME_KEY);
    applyTheme(next);
    render();
  });

  render();
}

setupThemeToggle();
