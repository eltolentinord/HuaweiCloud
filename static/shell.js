/* Shell común: barra lateral como panel en móvil/tablet (botón #sidebarToggle). Sin dependencias. */
(function () {
  "use strict";
  const sidebar = document.getElementById("sidebar");
  const toggle = document.getElementById("sidebarToggle");
  if (!sidebar || !toggle) return;
  let backdrop = null;

  function close() {
    sidebar.classList.remove("sidebar-open");
    toggle.setAttribute("aria-expanded", "false");
    if (backdrop) { backdrop.remove(); backdrop = null; }
  }
  function open() {
    sidebar.classList.add("sidebar-open");
    toggle.setAttribute("aria-expanded", "true");
    backdrop = document.createElement("div");
    backdrop.className = "sidebar-backdrop lg:hidden";
    backdrop.addEventListener("click", close);
    document.body.appendChild(backdrop);
  }
  toggle.addEventListener("click", () => (sidebar.classList.contains("sidebar-open") ? close() : open()));
  sidebar.addEventListener("click", (e) => { if (e.target.closest("a")) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  window.addEventListener("resize", () => { if (window.innerWidth >= 1024) close(); });
})();
