/* BizManager Multi-Shop ERP — main.js */

function toggleSidebar() {
  const s = document.getElementById("sidebar");
  const o = document.getElementById("overlay");
  if (s) s.classList.toggle("open");
  if (o) o.classList.toggle("open");
}

function toggleDark() {
  const html = document.documentElement;
  const dark  = html.getAttribute("data-theme") === "dark";
  html.setAttribute("data-theme", dark ? "light" : "dark");
  localStorage.setItem("bms_theme", dark ? "light" : "dark");
  updateDarkBtn();
}

function updateDarkBtn() {
  const btn = document.getElementById("darkBtn");
  if (!btn) return;
  btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
}

// Apply saved theme immediately
(function(){
  const saved = localStorage.getItem("bms_theme") || "light";
  document.documentElement.setAttribute("data-theme", saved);
  document.addEventListener("DOMContentLoaded", updateDarkBtn);
})();

// Auto-dismiss alerts after 4 s
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".alert").forEach(a => {
    setTimeout(() => {
      a.style.transition = "opacity .5s";
      a.style.opacity    = "0";
      setTimeout(() => a.remove(), 500);
    }, 4000);
  });
});

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
