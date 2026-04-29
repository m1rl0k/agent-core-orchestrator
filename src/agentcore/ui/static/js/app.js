// agentcore UI — tiny vanilla JS. Activates Lucide icons, light polling
// on /jobs and /chains pages, and the theme toggle.

(function () {
  if (window.lucide && typeof window.lucide.createIcons === "function") {
    window.lucide.createIcons();
  }

  // Theme toggle — persisted in localStorage. Falls back to OS pref.
  const stored = localStorage.getItem("agentcore-theme");
  if (stored) document.documentElement.setAttribute("data-theme", stored);
  document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("agentcore-theme", next);
    });
  });

  // Auto-refresh for pages that opt in with <body data-refresh="10000">.
  const refresh = parseInt(document.body.dataset.refresh || "0", 10);
  if (refresh > 0) {
    setTimeout(() => window.location.reload(), refresh);
  }
})();
