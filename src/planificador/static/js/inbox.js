/* Comentarios (inbox) — small behaviours on top of htmx (doc 05 §5).
 *
 * - Alpine component inboxThread(rootId, max): reply target ("Respondiendo a …"), counter, Ctrl+Enter.
 * - Selecting a conversation highlights it in the list and remembers it in #inbox-filters [name=c] so list
 *   refreshes (search, "Actualizar", inbox:changed) keep the highlight.
 * - Narrow screens: list and conversation are stacked; .has-thread on #inbox-layout shows the conversation.
 */
(function () {
  "use strict";

  const doc = document;

  function layout() {
    return doc.getElementById("inbox-layout");
  }

  function selectedInput() {
    return doc.querySelector("#inbox-filters [data-inbox-selected]");
  }

  function highlight(id) {
    doc.querySelectorAll(".ib-item.is-active").forEach((el) => {
      el.classList.remove("is-active");
      const link = el.querySelector(".ib-item__link");
      if (link) link.removeAttribute("aria-current");
    });
    const input = selectedInput();
    if (input) input.value = id ? String(id) : "";
    const box = layout();
    if (box) box.classList.toggle("has-thread", Boolean(id));
    if (!id) return;
    const item = doc.getElementById("inbox-item-" + id);
    if (item) {
      item.classList.add("is-active");
      const link = item.querySelector(".ib-item__link");
      if (link) link.setAttribute("aria-current", "true");
    }
  }

  function scrollToLatest(root) {
    const scroller = root && root.querySelector("[data-inbox-scroll]");
    if (scroller && scroller.scrollHeight > scroller.clientHeight) scroller.scrollTop = scroller.scrollHeight;
  }

  // Clicking a conversation: highlight right away (the request then fills the right panel).
  doc.addEventListener("click", (event) => {
    const link = event.target.closest(".ib-item__link[data-thread-id]");
    if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) {
      highlight(link.dataset.threadId);
    }
    const back = event.target.closest("[data-inbox-back]");
    if (back) {
      event.preventDefault();
      const box = layout();
      if (box) box.classList.remove("has-thread");
    }
  });

  doc.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail && event.detail.target;
    if (!target) return;
    if (target.id === "inbox-thread") {
      const article = target.querySelector(".ib-thread[data-thread-id]");
      highlight(article ? article.dataset.threadId : null);
      if (article) scrollToLatest(target);
    }
  });

  // Back/forward restores the whole page: re-apply the narrow-screen state.
  doc.addEventListener("DOMContentLoaded", () => {
    const article = doc.querySelector("#inbox-thread .ib-thread[data-thread-id]");
    if (article) scrollToLatest(doc.getElementById("inbox-thread"));
  });

  doc.addEventListener("alpine:init", () => {
    window.Alpine.data("inboxThread", (rootId, max) => ({
      target: rootId,
      targetName: "",
      text: "",
      max: max || 2200,
      replyTo(id, name, isRoot) {
        this.target = id;
        this.targetName = isRoot ? "" : name || "";
        this.$nextTick(() => {
          const area = this.$refs.text;
          if (area) {
            area.focus();
            area.scrollIntoView({ block: "nearest" });
          }
        });
      },
      clearTarget() {
        this.target = rootId;
        this.targetName = "";
      },
      submit(event) {
        const form = event.target.closest("form");
        if (form && this.text.trim() && this.text.length <= this.max) form.requestSubmit();
      },
    }));
  });
})();
