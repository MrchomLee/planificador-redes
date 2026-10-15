/* Planificador — helpers for Ajustes, Marcas and Cuentas conectadas (loaded before Alpine).
 *
 *  - Time-zone picker: <div data-tz-picker> <input data-tz-filter> <select> filters the IANA zones as you type
 *    (accent-insensitive, Spanish country/city aliases) and selects the first match.
 *  - Ajustes side navigation: highlights the section in view ([data-settings-nav] + IntersectionObserver).
 *  - Alpine component "metaChooser" (Meta page chooser): Instagram switch follows the selected page.
 *  - Browser-notification status text for the "Avisos" section.
 * Everything uses event delegation, so content swapped in by htmx works without re-initialisation.
 */
(function () {
  "use strict";

  const doc = document;

  // ------------------------------------------------------------------ time-zone filter
  const ALIASES = {
    espana: "Europe/Madrid",
    peninsula: "Europe/Madrid",
    madrid: "Europe/Madrid",
    barcelona: "Europe/Madrid",
    canarias: "Atlantic/Canary",
    tenerife: "Atlantic/Canary",
    "gran canaria": "Atlantic/Canary",
    mexico: "America/Mexico_City",
    cdmx: "America/Mexico_City",
    guadalajara: "America/Mexico_City",
    monterrey: "America/Monterrey",
    cancun: "America/Cancun",
    tijuana: "America/Tijuana",
    colombia: "America/Bogota",
    peru: "America/Lima",
    chile: "America/Santiago",
    argentina: "America/Argentina/Buenos_Aires",
    venezuela: "America/Caracas",
    uruguay: "America/Montevideo",
    paraguay: "America/Asuncion",
    bolivia: "America/La_Paz",
    ecuador: "America/Guayaquil",
    guatemala: "America/Guatemala",
    "costa rica": "America/Costa_Rica",
    panama: "America/Panama",
    "republica dominicana": "America/Santo_Domingo",
    "puerto rico": "America/Puerto_Rico",
    cuba: "America/Havana",
    "el salvador": "America/El_Salvador",
    honduras: "America/Tegucigalpa",
    nicaragua: "America/Managua",
    "nueva york": "America/New_York",
    miami: "America/New_York",
    "los angeles": "America/Los_Angeles",
    londres: "Europe/London",
    "reino unido": "Europe/London",
    lisboa: "Europe/Lisbon",
    portugal: "Europe/Lisbon",
    paris: "Europe/Paris",
    francia: "Europe/Paris",
    berlin: "Europe/Berlin",
    alemania: "Europe/Berlin",
    roma: "Europe/Rome",
    italia: "Europe/Rome",
    "guinea ecuatorial": "Africa/Malabo",
    marruecos: "Africa/Casablanca",
  };

  function normalize(s) {
    return String(s || "")
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[_/]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function filterTimezones(input) {
    const picker = input.closest("[data-tz-picker]");
    const select = picker && picker.querySelector("select");
    if (!select) return;
    const q = normalize(input.value);
    const aliasHits = new Set(
      Object.keys(ALIASES)
        .filter((k) => q && (k.includes(q) || q.includes(k)))
        .map((k) => ALIASES[k])
    );
    let first = null;
    let aliasFirst = null;
    let count = 0;
    select.querySelectorAll("optgroup").forEach((group) => {
      let visible = 0;
      group.querySelectorAll("option").forEach((opt) => {
        const hit = !q || aliasHits.has(opt.value) || normalize(opt.value + " " + opt.textContent).includes(q);
        opt.hidden = !hit;
        if (hit) {
          visible += 1;
          count += 1;
          if (!first) first = opt;
          if (!aliasFirst && aliasHits.has(opt.value)) aliasFirst = opt;
        }
      });
      group.hidden = visible === 0;
    });
    const empty = picker.querySelector("[data-tz-empty]");
    if (empty) empty.hidden = count > 0;
    const current = select.selectedOptions[0];
    const pick = aliasFirst || first;
    if (q && pick && (!current || current.hidden || (aliasFirst && current.value !== aliasFirst.value))) {
      select.value = pick.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  doc.addEventListener("input", (e) => {
    const input = e.target.closest && e.target.closest("[data-tz-filter]");
    if (input) filterTimezones(input);
  });
  doc.addEventListener("keydown", (e) => {
    const input = e.target.closest && e.target.closest("[data-tz-filter]");
    if (!input) return;
    if (e.key === "Enter") {
      e.preventDefault(); // never submit the form from the filter box
      const select = input.closest("[data-tz-picker]").querySelector("select");
      if (select) select.focus();
    } else if (e.key === "Escape" && input.value) {
      e.preventDefault();
      e.stopPropagation();
      input.value = "";
      filterTimezones(input);
    }
  });

  // ------------------------------------------------------------------ Ajustes side navigation
  function initSettingsNav() {
    const nav = doc.querySelector("[data-settings-nav]");
    if (!nav || !("IntersectionObserver" in window)) return;
    const links = Array.from(nav.querySelectorAll("[data-section]"));
    const setActive = (key) =>
      links.forEach((a) => {
        const on = a.dataset.section === key;
        a.classList.toggle("is-active", on);
        if (on) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      });
    const visible = new Map();
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((en) => visible.set(en.target.id, en.isIntersecting ? en.intersectionRatio : 0));
        let best = null;
        links.forEach((a) => {
          const ratio = visible.get(a.dataset.section) || 0;
          if (ratio > 0 && (!best || ratio > best.ratio)) best = { key: a.dataset.section, ratio };
        });
        if (best) setActive(best.key);
      },
      { rootMargin: "-80px 0px -45% 0px", threshold: [0, 0.15, 0.4, 0.75] }
    );
    const observeAll = () => {
      observer.disconnect();
      links.forEach((a) => {
        const el = doc.getElementById(a.dataset.section);
        if (el) observer.observe(el);
      });
    };
    observeAll();
    // Sections are re-rendered by htmx (outerHTML): observe the new elements.
    doc.addEventListener("htmx:afterSettle", observeAll);
    links.forEach((a) => a.addEventListener("click", () => setActive(a.dataset.section)));
    const hash = (location.hash || "").replace("#", "");
    const match = hash && links.find((a) => hash === a.dataset.section || hash.startsWith(a.dataset.section + "-"));
    if (match) setActive(match.dataset.section);
  }

  // ------------------------------------------------------------------ notification permission text
  function syncNotifyStatus() {
    doc.querySelectorAll("[data-notify-status]").forEach((el) => {
      if (el.textContent.trim() && el.textContent.trim() !== "Comprobando…") return;
      if (!("Notification" in window)) el.textContent = "Este navegador no admite avisos.";
      else if (Notification.permission === "default") el.textContent = "Aún no has dado permiso a este navegador.";
      else if (Notification.permission === "granted") el.textContent = "Avisos del navegador activados";
      else el.textContent = "Avisos bloqueados en el navegador";
    });
  }

  // ------------------------------------------------------------------ Alpine components
  doc.addEventListener("alpine:init", () => {
    const Alpine = window.Alpine;
    // Meta page chooser: x-data='metaChooser({"page": "<id>", "ig": {"<page id>": true|false}, "focus": "facebook"})'
    Alpine.data("metaChooser", (cfg) => ({
      page: (cfg && cfg.page) || null,
      ig: (cfg && cfg.ig) || {},
      wantIg: true,
      get hasIg() {
        return Boolean(this.page && this.ig[this.page]);
      },
    }));
  });

  function init() {
    initSettingsNav();
    // app.js fills [data-notify-status] on load; give it a tick, then add the "default" wording.
    setTimeout(syncNotifyStatus, 0);
    doc.addEventListener("htmx:afterSettle", () => setTimeout(syncNotifyStatus, 0));
    // Deep links to a section (e.g. /ajustes#credenciales-meta) land below the sticky top bar.
    const hash = (location.hash || "").slice(1);
    if (hash) {
      const target = doc.getElementById(hash);
      if (target) requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
    }
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();
