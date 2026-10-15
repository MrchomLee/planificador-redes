/* Post detail drawer, "Publicación asistida" modal and notification centre helpers (loaded on every page).
 *
 * - Alpine component assistedChecklist(targetId): ticks of the assisted checklist, remembered per target in
 *   localStorage (App.storage) so closing and reopening the modal keeps them.
 * - HX-Trigger "post:open" {id}: opens that post in the drawer (used after "Duplicar").
 * - The drawer scrolls the highlighted target card (.ptarget.is-focus) into view when it opens.
 * - Broken thumbnails (img.js-pd-thumb) are removed so the icon placeholder underneath shows.
 */
(function () {
  "use strict";

  const doc = document;
  const App = (window.App = window.App || {});
  const CHECK_KEY = "assist.check.";

  function storageGet(key, fallback) {
    return App.storage ? App.storage.get(key, fallback) : fallback;
  }
  function storageSet(key, value) {
    if (App.storage) App.storage.set(key, value);
  }

  doc.addEventListener("alpine:init", () => {
    window.Alpine.data("assistedChecklist", (targetId) => ({
      done: [],
      init() {
        const saved = storageGet(CHECK_KEY + targetId, []);
        this.done = Array.isArray(saved) ? saved : [];
      },
      isDone(index) {
        return this.done.includes(index);
      },
      toggle(index, checked) {
        const set = new Set(this.done);
        if (checked) set.add(index);
        else set.delete(index);
        this.done = Array.from(set).sort((a, b) => a - b);
        storageSet(CHECK_KEY + targetId, this.done);
      },
    }));
  });

  // Open a post in the drawer (e.g. the copy created by "Duplicar").
  doc.addEventListener("post:open", (e) => {
    const detail = e.detail || {};
    const id = Number(detail.id || detail.value);
    if (!id || !window.htmx) return;
    window.htmx.ajax("GET", "/publicaciones/" + id, { target: "#drawer-root", swap: "innerHTML" });
  });

  function revealFocus(root) {
    const card = root && root.querySelector && root.querySelector(".ptarget.is-focus");
    if (!card) return;
    requestAnimationFrame(() => card.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  }
  doc.addEventListener("layer:open", (e) => {
    if (e.target && e.target.id === "drawer-root") revealFocus(e.target);
  });
  doc.addEventListener("DOMContentLoaded", () => revealFocus(doc.querySelector(".post-page")));

  // Thumbnails that fail to load: drop the <img>, the placeholder icon underneath stays.
  doc.addEventListener(
    "error",
    (e) => {
      const img = e.target;
      if (img instanceof HTMLImageElement && img.classList.contains("js-pd-thumb")) img.remove();
    },
    true
  );
})();
