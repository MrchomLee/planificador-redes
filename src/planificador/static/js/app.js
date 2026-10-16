/* Planificador — global UI helpers (loaded on every page, before Alpine).
 *
 * Exposes window.App:
 *   App.toast(message, kind?, opts?)        kind: info | success | warning | error
 *   App.modal.open(html?) / close() / isOpen()      (#modal-root; htmx swaps into it open it automatically)
 *   App.drawer.open(html?) / close() / isOpen()     (#drawer-root)
 *   App.confirm(message, {title, ok, cancel, danger}) -> Promise<boolean>
 *   App.copy(text)                          copy to clipboard + toast
 *   App.notify.enable() / process(items)    browser notifications ("Activar avisos")
 *   App.timeAgo(date) / App.refreshTimes(root?)
 *   App.fetchJSON(url, {method, body, headers})   CSRF header included
 *   App.csrfToken()
 *   App.icon(name)                          small set of inline icons (see partials/js_templates.html)
 *   App.spriteIcon(name, size)              any lucide icon via the vendored sprite (<use href>)
 *   App.theme.get() / set('auto'|'light'|'dark')   fires window "themechange"
 *   App.chartTheme()                        colours from CSS tokens for Chart.js
 *   App.applyChartDefaults()                Chart.defaults: es-ES locale, token colours (auto on load/themechange)
 *   App.formatNumber(n, decimals)
 *
 * Server-side contracts:
 *   - CSRF: every htmx request carries the X-CSRF-Token header from <meta name="csrf-token">.
 *   - HX-Trigger events understood: {"toast": {"message": "...", "kind": "success"}}, "closeModal", "closeDrawer",
 *     "notifications:refresh".
 *   - Error responses (4xx/5xx except 422) show a toast. Message source, in order: header X-Error-Message
 *     (URI-encoded), JSON {"detail": "..."}, short text/plain body, generic Spanish message by status.
 *   - 422 responses ARE swapped (validation errors re-rendered by the server).
 *   - Confirmation: hx-confirm="¿Seguro?" (+ optional data-confirm-title, data-confirm-ok, data-confirm-danger)
 *     uses the styled dialog; plain forms/links can use data-confirm="..." the same way.
 *   - Copy: any element with data-copy="text" or data-copy-target="#selector".
 *   - Relative times: <time datetime="ISO" data-timeago> is refreshed every minute.
 *   - Notifications: clicking an element with data-notif-read="<id>" marks that notification as read
 *     (POST /notificaciones/<id>/leer, fire and forget) while the click carries on (link / htmx request).
 *     The bell summary carries data-notify-items (browser notifications) and data-unread (tab title counter).
 */
(function () {
  "use strict";

  const App = (window.App = window.App || {});
  const doc = document;
  const STORAGE_PREFIX = "planificador.";

  // ------------------------------------------------------------------ small utils
  const $ = (sel, root) => (root || doc).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || doc).querySelectorAll(sel));

  App.storage = {
    get(key, fallback) {
      try {
        const raw = window.localStorage.getItem(STORAGE_PREFIX + key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (e) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        window.localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(value));
      } catch (e) {
        /* storage unavailable (private mode, blocked): ignore */
      }
    },
  };

  App.csrfToken = function () {
    const meta = $('meta[name="csrf-token"]');
    return (meta && meta.content) || "";
  };

  App.escape = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  };

  App.formatNumber = function (n, decimals) {
    if (n === null || n === undefined || n === "" || isNaN(n)) return "—";
    return Number(n).toLocaleString("es-ES", {
      minimumFractionDigits: decimals || 0,
      maximumFractionDigits: decimals || 0,
      useGrouping: "always",
    });
  };

  // Icons pre-rendered by the server (partials/js_templates.html).
  App.icon = function (name) {
    const tpl = $('#js-icons [data-icon="' + name + '"]');
    return tpl ? tpl.innerHTML : "";
  };

  App.spriteIcon = function (name, size) {
    const s = size || 18;
    const sprite = doc.body ? doc.body.dataset.sprite : "";
    return (
      '<svg class="icon icon-' + App.escape(name) + '" width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<use href="' + App.escape(sprite) + "#" + App.escape(name) + '"></use></svg>'
    );
  };

  // ------------------------------------------------------------------ toasts
  const TOAST_ICONS = { info: "info", success: "success", warning: "warning", error: "error" };
  const TOAST_TONES = { info: "tone-info", success: "tone-success", warning: "tone-warning", error: "tone-danger" };
  const TOAST_TIMES = { info: 4500, success: 4000, warning: 7000, error: 9000 };

  App.toast = function (message, kind, opts) {
    const region = $("#toast-root");
    if (!region || !message) return null;
    kind = TOAST_TONES[kind] ? kind : kind === "danger" ? "error" : "info";
    opts = opts || {};
    const el = doc.createElement("div");
    el.className = "toast " + TOAST_TONES[kind];
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.innerHTML =
      '<span class="toast__icon">' + App.icon(TOAST_ICONS[kind]) + "</span>" +
      '<div class="toast__body">' +
      (opts.title ? '<div class="toast__title">' + App.escape(opts.title) + "</div>" : "") +
      '<div class="toast__msg"></div></div>' +
      '<button type="button" class="icon-btn icon-btn--sm toast__close" aria-label="Cerrar aviso">' + App.icon("x") + "</button>";
    $(".toast__msg", el).textContent = message;
    region.appendChild(el);
    while (region.children.length > 4) region.firstElementChild.remove();

    let timer = null;
    const remove = () => {
      if (!el.isConnected) return;
      el.classList.add("is-leaving");
      setTimeout(() => el.remove(), 190);
    };
    const arm = () => {
      const ms = opts.duration === undefined ? TOAST_TIMES[kind] : opts.duration;
      if (ms) timer = setTimeout(remove, ms);
    };
    el.addEventListener("mouseenter", () => clearTimeout(timer));
    el.addEventListener("mouseleave", arm);
    $(".toast__close", el).addEventListener("click", remove);
    arm();
    return el;
  };

  // ------------------------------------------------------------------ layers (modal, drawer, confirm)
  // Each layer root holds at most one dialog. The topmost open layer receives Esc / focus trapping.
  const LAYER_IDS = ["confirm-root", "modal-root", "drawer-root"]; // priority order (top first)
  const lastFocus = {};
  const lastFocusLoc = {};

  // ------------------------------------------------------------------ focus that survives re-renders
  // htmx swaps and FullCalendar refetches replace elements (list rows, calendar events) with new copies. A
  // locator (id, else hx-get, else data-post-id, plus the class) finds the new copy so focus can go back to it.
  const LOCATOR_ATTRS = ["id", "hx-get", "data-post-id"];
  App.focusLocator = function (el) {
    if (!el || el === doc.body || el === doc.documentElement || !el.getAttribute) return null;
    const attr = LOCATOR_ATTRS.find((a) => el.getAttribute(a));
    return attr ? { attr: attr, value: el.getAttribute(attr), cls: el.className || "" } : null;
  };
  App.findByLocator = function (loc) {
    if (!loc) return null;
    const value = window.CSS && CSS.escape ? CSS.escape(loc.value) : loc.value.replace(/["\\]/g, "\\$&");
    const sel = loc.attr === "id" ? "#" + value : "[" + loc.attr + '="' + value + '"]';
    let found = [];
    try {
      found = $$(sel).filter((el) => el.getClientRects().length && !el.closest("[hidden], [inert]"));
    } catch (e) {
      return null;
    }
    // Same kind of element first (the row's open button rather than a hidden menu item with the same hx-get).
    return found.find((el) => (el.className || "") === loc.cls) || found[0] || null;
  };

  function layerRoot(id) {
    return doc.getElementById(id);
  }
  function layerOpen(id) {
    const root = layerRoot(id);
    return !!(root && root.firstElementChild && !root.classList.contains("layer-closing"));
  }
  function topLayer() {
    return LAYER_IDS.find(layerOpen) || null;
  }
  function dialogOf(root) {
    return root && (root.querySelector('[role="dialog"], [role="alertdialog"]') || root.firstElementChild);
  }
  function focusables(container) {
    return $$(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
        'textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), [contenteditable="true"]',
      container
    ).filter((el) => el.offsetParent !== null || el === doc.activeElement);
  }
  function syncScrollLock() {
    doc.documentElement.classList.toggle("has-layer", !!topLayer());
  }

  function activateLayer(id) {
    const root = layerRoot(id);
    if (!root || !root.firstElementChild) return;
    root.classList.remove("layer-closing");
    if (!lastFocus[id] || !doc.contains(lastFocus[id])) {
      const active = doc.activeElement;
      lastFocus[id] = active && !root.contains(active) ? active : null;
      lastFocusLoc[id] = App.focusLocator(lastFocus[id]);
    }
    syncScrollLock();
    const dialog = dialogOf(root);
    requestAnimationFrame(() => {
      if (!dialog || dialog.contains(doc.activeElement)) return;
      const auto = dialog.querySelector("[autofocus]");
      const first = auto || focusables(dialog).find((el) => !el.matches("[data-close]"));
      (first || dialog).focus({ preventScroll: true });
    });
    App.refreshTimes(root);
    root.dispatchEvent(new CustomEvent("layer:open", { bubbles: true }));
  }

  function closeLayer(id, opts) {
    const root = layerRoot(id);
    if (!root || !root.firstElementChild) return true;
    const force = opts && opts.force;
    if (!force) {
      const ev = new CustomEvent("layer:beforeclose", { bubbles: true, cancelable: true });
      if (!root.firstElementChild.dispatchEvent(ev)) return false; // a page script vetoed (e.g. unsaved changes)
    }
    root.classList.add("layer-closing");
    const closing = root.firstElementChild;
    const finish = () => {
      // New content may have been swapped in while the closing animation ran: keep it.
      if (root.firstElementChild !== closing) return;
      root.classList.remove("layer-closing");
      root.innerHTML = "";
      syncScrollLock();
      // The opener may have been re-rendered while the layer was open (a refresh after an action): use its copy.
      const back = lastFocus[id] && doc.contains(lastFocus[id]) ? lastFocus[id] : App.findByLocator(lastFocusLoc[id]);
      lastFocus[id] = null;
      lastFocusLoc[id] = null;
      if (back) back.focus({ preventScroll: true });
      root.dispatchEvent(new CustomEvent("layer:closed", { bubbles: true }));
    };
    const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) finish();
    else setTimeout(finish, 150);
    return true;
  }

  function openLayer(id, html) {
    const root = layerRoot(id);
    if (!root) return;
    if (html !== undefined) {
      root.innerHTML = html;
      if (window.htmx) window.htmx.process(root);
    }
    activateLayer(id);
  }

  App.modal = {
    open: (html) => openLayer("modal-root", html),
    close: (opts) => closeLayer("modal-root", opts),
    isOpen: () => layerOpen("modal-root"),
  };
  App.drawer = {
    open: (html) => openLayer("drawer-root", html),
    close: (opts) => closeLayer("drawer-root", opts),
    isOpen: () => layerOpen("drawer-root"),
  };
  App.layers = { top: topLayer, close: closeLayer, open: openLayer };

  // ------------------------------------------------------------------ confirm dialog
  let confirmResolve = null;

  App.confirm = function (message, opts) {
    opts = opts || {};
    const root = layerRoot("confirm-root");
    const tpl = $("#tpl-confirm");
    if (!root || !tpl) return Promise.resolve(window.confirm(message));
    if (confirmResolve) confirmResolve(false);
    root.innerHTML = tpl.innerHTML;
    const danger = !!opts.danger;
    const box = $(".confirm", root) || root;
    box.classList.add(danger ? "tone-danger" : "tone-primary");
    $(".confirm__icon", root).innerHTML = App.icon(danger ? "warning" : "question");
    $(".confirm__title", root).textContent = opts.title || "¿Seguro?";
    $(".confirm__message", root).textContent = message || "";
    const ok = $("[data-confirm-accept]", root);
    ok.textContent = opts.ok || (danger ? "Sí, continuar" : "Aceptar");
    ok.classList.toggle("btn--danger", danger);
    ok.classList.toggle("btn--primary", !danger);
    $("[data-confirm-dismiss]", root).textContent = opts.cancel || "Cancelar";
    activateLayer("confirm-root");
    requestAnimationFrame(() => ok.focus());
    return new Promise((resolve) => {
      confirmResolve = (value) => {
        confirmResolve = null;
        closeLayer("confirm-root", { force: true });
        resolve(value);
      };
    });
  };

  function confirmOptionsFrom(el) {
    const src = (el && el.closest("[data-confirm-title], [data-confirm-ok], [data-confirm-danger], [hx-confirm], [data-confirm]")) || el;
    if (!src) return {};
    return {
      title: src.getAttribute("data-confirm-title") || undefined,
      ok: src.getAttribute("data-confirm-ok") || undefined,
      cancel: src.getAttribute("data-confirm-cancel") || undefined,
      danger: src.hasAttribute("data-confirm-danger"),
    };
  }

  // ------------------------------------------------------------------ clipboard
  App.copy = async function (text, okMessage) {
    const value = String(text == null ? "" : text);
    let done = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        done = true;
      }
    } catch (e) {
      done = false;
    }
    if (!done) {
      const ta = doc.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
      doc.body.appendChild(ta);
      ta.select();
      try {
        done = doc.execCommand("copy");
      } catch (e) {
        done = false;
      }
      ta.remove();
    }
    if (done) App.toast(okMessage || "Copiado al portapapeles", "success");
    else App.toast("No se ha podido copiar. Selecciona el texto y pulsa Ctrl+C.", "warning");
    return done;
  };
  App.copyToClipboard = App.copy;

  // ------------------------------------------------------------------ relative time ("hace 5 min")
  App.timeAgo = function (value, now) {
    const d = value instanceof Date ? value : new Date(value);
    if (isNaN(d)) return "";
    const seconds = ((now || new Date()) - d) / 1000;
    const future = seconds < 0;
    const s = Math.abs(seconds);
    const minutes = s / 60;
    const hours = minutes / 60;
    const days = hours / 24;
    const rel = (t) => (future ? "en " : "hace ") + t;
    if (s < 45) return future ? "en un momento" : "hace un momento";
    if (minutes < 60) return rel(Math.max(1, Math.round(minutes)) + " min");
    if (hours < 24) return rel(Math.max(1, Math.round(hours)) + " h");
    if (days < 2) return future ? "mañana" : "ayer";
    if (days < 30) return rel(Math.floor(days) + " días");
    if (days < 365) {
      const m = Math.max(1, Math.floor(days / 30));
      return rel(m === 1 ? "1 mes" : m + " meses");
    }
    const y = Math.max(1, Math.floor(days / 365));
    return rel(y === 1 ? "1 año" : y + " años");
  };

  App.refreshTimes = function (root) {
    $$("time[data-timeago]", root).forEach((el) => {
      const text = App.timeAgo(el.getAttribute("datetime"));
      if (text && el.textContent !== text) el.textContent = text;
    });
  };

  // ------------------------------------------------------------------ fetch helper
  App.fetchJSON = async function (url, opts) {
    opts = opts || {};
    const headers = Object.assign({ Accept: "application/json", "X-CSRF-Token": App.csrfToken() }, opts.headers || {});
    let body = opts.body;
    if (body && !(body instanceof FormData) && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const resp = await fetch(url, { method: opts.method || (body ? "POST" : "GET"), headers, body, credentials: "same-origin" });
    const type = resp.headers.get("Content-Type") || "";
    const data = type.includes("application/json") ? await resp.json().catch(() => null) : await resp.text();
    if (!resp.ok) {
      const err = new Error(errorMessageFrom(resp.status, resp.headers.get("X-Error-Message"), type, data));
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  };

  // ------------------------------------------------------------------ error messages
  const STATUS_MESSAGES = {
    0: "No se ha podido conectar con la aplicación. ¿Sigue abierta la ventana del servidor?",
    400: "La petición no es válida.",
    401: "Tu sesión ha caducado. Vuelve a entrar.",
    403: "No se ha podido verificar la petición. Recarga la página e inténtalo de nuevo.",
    404: "No se ha encontrado lo que buscabas. Puede que se haya borrado.",
    408: "La operación ha tardado demasiado. Inténtalo de nuevo.",
    409: "Los datos han cambiado mientras tanto. Recarga la página.",
    413: "El archivo es demasiado grande.",
    415: "Ese tipo de archivo no está admitido.",
    422: "Revisa los datos del formulario.",
    429: "Demasiadas peticiones seguidas. Espera un momento.",
    500: "Ha ocurrido un error inesperado. Revisa el registro de la aplicación.",
    502: "Un servicio externo no ha respondido correctamente.",
    503: "La aplicación está ocupada. Inténtalo en unos segundos.",
  };

  function errorMessageFrom(status, headerMsg, contentType, body) {
    if (headerMsg) {
      try {
        return decodeURIComponent(headerMsg);
      } catch (e) {
        return headerMsg;
      }
    }
    let data = body;
    if (typeof body === "string" && (contentType || "").includes("application/json")) {
      try {
        data = JSON.parse(body);
      } catch (e) {
        data = null;
      }
    }
    if (data && typeof data === "object") {
      if (typeof data.detail === "string") return data.detail;
      if (typeof data.message === "string") return data.message;
      if (Array.isArray(data.detail)) return STATUS_MESSAGES[422];
    }
    if (typeof body === "string" && (contentType || "").includes("text/plain") && body.length && body.length < 300) {
      return body;
    }
    return STATUS_MESSAGES[status] || (status >= 500 ? STATUS_MESSAGES[500] : "No se ha podido completar la acción (" + status + ").");
  }
  App.errorMessage = errorMessageFrom;

  // ------------------------------------------------------------------ browser notifications
  const NOTIFY_KINDS = ["manual_publish", "error", "unknown", "missed", "reconnect"];

  App.notify = {
    supported: () => "Notification" in window,
    permission: () => ("Notification" in window ? Notification.permission : "unsupported"),
    async enable() {
      if (!App.notify.supported()) {
        App.toast("Este navegador no permite avisos de escritorio.", "warning");
        return false;
      }
      let result = Notification.permission;
      if (result === "default") {
        try {
          result = await Notification.requestPermission();
        } catch (e) {
          result = "denied";
        }
      }
      App.notify.syncUI();
      if (result === "granted") {
        App.toast("Avisos activados. Te avisaremos cuando toque publicar.", "success");
        return true;
      }
      App.toast("El navegador ha bloqueado los avisos. Puedes permitirlos en la configuración del sitio.", "warning");
      return false;
    },
    // items: [{id, kind, title, body, url}] newest first or any order.
    process(items) {
      if (!Array.isArray(items) || !items.length) return;
      const maxId = Math.max.apply(null, items.map((i) => Number(i.id) || 0));
      const lastSeen = App.storage.get("notif.lastSeen", null);
      App.storage.set("notif.lastSeen", Math.max(maxId, Number(lastSeen) || 0));
      if (lastSeen === null) return; // first run: do not flood with old items
      if (App.notify.permission() !== "granted") return;
      if (doc.body && doc.body.dataset.browserNotify === "0") return; // disabled in Ajustes
      items
        .filter((i) => Number(i.id) > Number(lastSeen) && NOTIFY_KINDS.includes(i.kind))
        .slice(0, 3)
        .forEach((i) => {
          try {
            const n = new Notification(i.title || "Planificador", {
              body: i.body || "",
              tag: "planificador-" + i.id,
              icon: doc.body.dataset.favicon || undefined,
              // "Publicación asistida" needs the user: keep it on screen until it is dismissed.
              requireInteraction: i.kind === "manual_publish",
            });
            n.onclick = () => {
              window.focus();
              App.notify.markRead(i.id);
              if (i.url) window.location.href = i.url;
              n.close();
            };
          } catch (e) {
            /* some browsers only allow notifications from a service worker */
          }
        });
    },
    // Offer browser notifications once per session when something urgent arrives and the user never decided.
    nudge(items) {
      if (App.notify.permission() !== "default" || !Array.isArray(items)) return;
      if (doc.body && doc.body.dataset.browserNotify === "0") return;
      if (!items.some((i) => i.kind === "manual_publish" || i.kind === "error")) return;
      try {
        if (window.sessionStorage.getItem(STORAGE_PREFIX + "notif.nudged")) return;
        window.sessionStorage.setItem(STORAGE_PREFIX + "notif.nudged", "1");
      } catch (e) {
        return;
      }
      App.toast("Pulsa «Activar avisos» en la campana para enterarte aunque estés en otra pestaña.", "info", {
        title: "Avisos del navegador",
      });
    },
    markRead(id) {
      if (!id) return;
      try {
        fetch("/notificaciones/" + encodeURIComponent(id) + "/leer", {
          method: "POST",
          headers: { "X-CSRF-Token": App.csrfToken(), Accept: "application/json" },
          credentials: "same-origin",
          keepalive: true,
        })
          .then(() => {
            if (window.htmx) window.htmx.trigger(doc.body, "notifications:refresh");
          })
          .catch(() => {});
      } catch (e) {
        /* ignore */
      }
    },
    // "(3) Planificación · Planificador" while there are unread notifications.
    setUnread(count) {
      const n = Math.max(0, Number(count) || 0);
      if (App.notify.baseTitle === undefined) App.notify.baseTitle = doc.title.replace(/^\(\d+\+?\)\s+/, "");
      doc.title = (n ? "(" + (n > 99 ? "99+" : n) + ") " : "") + App.notify.baseTitle;
    },
    syncUI() {
      const perm = App.notify.permission();
      $$("[data-notify-enable], [data-notify-prompt]").forEach((el) => (el.hidden = perm !== "default"));
      $$("[data-notify-status]").forEach((el) => {
        el.textContent =
          perm === "granted" ? "Avisos del navegador activados" : perm === "denied" ? "Avisos bloqueados en el navegador" : perm === "unsupported" ? "Avisos no disponibles" : "";
      });
    },
  };

  function readNotifyPayload(root) {
    const holder = root && (root.matches && root.matches("[data-notify-items]") ? root : $("[data-notify-items]", root));
    if (!holder) return;
    if (holder.hasAttribute("data-unread")) App.notify.setUnread(holder.getAttribute("data-unread"));
    try {
      const items = JSON.parse(holder.getAttribute("data-notify-items") || "[]");
      const lastSeen = App.storage.get("notif.lastSeen", null);
      App.notify.process(items);
      if (lastSeen !== null) App.notify.nudge(items.filter((i) => Number(i.id) > Number(lastSeen)));
    } catch (e) {
      /* malformed payload: ignore */
    }
  }

  // ------------------------------------------------------------------ theme
  const THEMES = ["auto", "light", "dark"];
  const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  App.theme = {
    get: () => {
      const v = App.storage.get("theme", "auto");
      return THEMES.includes(v) ? v : "auto";
    },
    set(pref) {
      pref = THEMES.includes(pref) ? pref : "auto";
      App.storage.set("theme", pref);
      if (pref === "auto") delete doc.documentElement.dataset.theme;
      else doc.documentElement.dataset.theme = pref;
      window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: App.theme.effective() } }));
    },
    effective() {
      const forced = doc.documentElement.dataset.theme;
      if (forced === "light" || forced === "dark") return forced;
      return darkQuery && darkQuery.matches ? "dark" : "light";
    },
  };
  if (darkQuery && darkQuery.addEventListener) {
    darkQuery.addEventListener("change", () => {
      if (App.theme.get() === "auto") window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: App.theme.effective() } }));
    });
  }

  App.chartTheme = function () {
    const cs = getComputedStyle(doc.documentElement);
    const v = (name) => cs.getPropertyValue(name).trim();
    return {
      series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v("--chart-" + i)),
      sequential: [1, 2, 3, 4, 5, 6, 7].map((i) => v("--chart-seq-" + i)),
      grid: v("--chart-grid"),
      axis: v("--chart-axis"),
      text: v("--chart-text"),
      ink: v("--text"),
      surface: v("--surface"),
      good: v("--chart-good"),
      bad: v("--chart-bad"),
      font: v("--font-sans"),
    };
  };

  // ------------------------------------------------------------------ htmx integration
  function isTyping(el) {
    if (!el) return false;
    const tag = el.tagName;
    return el.isContentEditable || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  let inflight = 0;
  function setLoading(delta) {
    inflight = Math.max(0, inflight + delta);
    if (doc.body) doc.body.classList.toggle("is-loading", inflight > 0);
  }

  doc.addEventListener("htmx:configRequest", (e) => {
    const token = App.csrfToken();
    if (token) e.detail.headers["X-CSRF-Token"] = token;
  });
  doc.addEventListener("htmx:beforeRequest", () => setLoading(1));
  doc.addEventListener("htmx:afterRequest", () => setLoading(-1));

  doc.addEventListener("htmx:responseError", (e) => {
    const xhr = e.detail.xhr;
    if (!xhr) return;
    const elt = e.detail.elt;
    if (elt && elt.closest && elt.closest("[data-silent-errors]")) return;
    const msg = errorMessageFrom(xhr.status, xhr.getResponseHeader("X-Error-Message"), xhr.getResponseHeader("Content-Type"), xhr.responseText);
    App.toast(msg, xhr.status >= 500 ? "error" : "warning");
  });
  doc.addEventListener("htmx:sendError", (e) => {
    const elt = e.detail && e.detail.elt;
    if (elt && elt.closest && elt.closest("[data-silent-errors]")) return;
    App.toast(STATUS_MESSAGES[0], "error");
  });
  doc.addEventListener("htmx:timeout", () => App.toast(STATUS_MESSAGES[408], "warning"));

  // Background requests (bell polling, self-refreshing panels: elements inside [data-silent-errors]) must not
  // drive navigation. When the session ends (optional password) the server answers them with HX-Redirect to
  // /login?next=<API path>: go to the login page for the *current page* instead, and never while the page is
  // already navigating away (e.g. "Cerrar sesión").
  let unloading = false;
  window.addEventListener("beforeunload", () => (unloading = true));
  window.addEventListener("pageshow", () => (unloading = false));
  doc.addEventListener("htmx:beforeOnLoad", (e) => {
    const xhr = e.detail && e.detail.xhr;
    const elt = e.detail && e.detail.elt;
    if (!xhr || !elt || !elt.closest || !elt.closest("[data-silent-errors]")) return;
    const target = xhr.getResponseHeader("HX-Redirect");
    if (!target) return;
    e.preventDefault();
    if (unloading || !/^\/login(\?|$)/.test(target)) return;
    window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname + window.location.search);
  });

  // Styled confirmation for hx-confirm.
  doc.addEventListener("htmx:confirm", (e) => {
    const question = e.detail.question;
    if (!question) return;
    e.preventDefault();
    App.confirm(question, confirmOptionsFrom(e.detail.elt)).then((ok) => {
      if (ok) e.detail.issueRequest(true);
    });
  });

  // A full HTML document aimed at a layer root (e.g. a route that does not answer HX-Request with a
  // fragment yet) is opened as a normal page instead of being nested inside the modal/drawer.
  doc.addEventListener("htmx:beforeSwap", (e) => {
    const target = e.detail.target;
    const xhr = e.detail.xhr;
    if (!target || !xhr || !e.detail.shouldSwap) return;
    if (target.id !== "modal-root" && target.id !== "drawer-root") return;
    if (/^\s*(<!doctype html|<html)/i.test(xhr.responseText || "")) {
      e.detail.shouldSwap = false;
      const info = e.detail.pathInfo || {};
      const path = info.responsePath || info.finalRequestPath || info.requestPath || null;
      if (path) window.location.href = path;
    }
  });

  // Content swapped into a layer root opens that layer.
  doc.addEventListener("htmx:afterSwap", (e) => {
    const target = e.detail.target;
    if (!target) return;
    if (target.id === "modal-root" || target.id === "drawer-root") {
      if (target.firstElementChild) activateLayer(target.id);
      else closeLayer(target.id, { force: true });
    }
    if (target.id === "notif-badge" || (target.closest && target.closest("#notif-badge"))) readNotifyPayload(target);
  });
  doc.addEventListener("htmx:afterSettle", (e) => {
    App.refreshTimes(e.detail.target || doc);
    App.notify.syncUI();
  });

  // Events from HX-Trigger response headers bubble to the triggering element/body.
  doc.addEventListener("toast", (e) => {
    const d = e.detail || {};
    const message = typeof d === "string" ? d : d.message || d.value;
    App.toast(message, d.kind || d.level || "success", { title: d.title });
  });
  doc.addEventListener("closeModal", () => App.modal.close({ force: true }));
  doc.addEventListener("closeDrawer", () => App.drawer.close({ force: true }));

  // ------------------------------------------------------------------ delegated clicks & keys
  doc.addEventListener("click", (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;

    // Confirm dialog buttons
    if (t.closest("[data-confirm-accept]") && confirmResolve) return confirmResolve(true);
    if (t.closest("[data-confirm-dismiss]") && confirmResolve) return confirmResolve(false);

    // Close buttons inside layers
    const closer = t.closest("[data-close], [data-modal-close], [data-drawer-close]");
    if (closer) {
      const root = closer.closest(".layer-root");
      if (root) {
        e.preventDefault();
        if (root.id === "confirm-root" && confirmResolve) confirmResolve(false);
        else closeLayer(root.id);
        return;
      }
    }
    // Click on the backdrop itself
    if (t.matches(".modal-backdrop, .drawer-backdrop") && !t.hasAttribute("data-static")) {
      const root = t.closest(".layer-root");
      if (root) {
        if (root.id === "confirm-root" && confirmResolve) confirmResolve(false);
        else closeLayer(root.id);
      }
      return;
    }

    // Copy helpers
    const copier = t.closest("[data-copy], [data-copy-target]");
    if (copier) {
      e.preventDefault();
      let text = copier.getAttribute("data-copy");
      const sel = copier.getAttribute("data-copy-target");
      if (sel) {
        const src = $(sel);
        text = src ? ("value" in src && src.value !== undefined ? src.value : src.textContent) : "";
      }
      App.copy(text, copier.getAttribute("data-copy-message") || undefined);
      return;
    }

    // Notification items: mark as read in the background, let the click continue.
    const notifEl = t.closest("[data-notif-read]");
    if (notifEl && notifEl.getAttribute("data-notif-read")) {
      const id = notifEl.getAttribute("data-notif-read");
      notifEl.removeAttribute("data-notif-read");
      notifEl.classList.remove("is-unread");
      App.notify.markRead(id);
    }

    // Browser notification permission
    if (t.closest("[data-notify-enable]")) {
      e.preventDefault();
      App.notify.enable();
      return;
    }

    // Theme buttons: data-theme-set="auto|light|dark"
    const themeBtn = t.closest("[data-theme-set]");
    if (themeBtn) {
      App.theme.set(themeBtn.getAttribute("data-theme-set"));
      syncThemeButtons();
      return;
    }

    // data-confirm on plain links / submit buttons (non-htmx)
    const confirmEl = t.closest("a[data-confirm], button[data-confirm]");
    if (confirmEl && !confirmEl.hasAttribute("hx-confirm") && !confirmEl.dataset.confirmed) {
      e.preventDefault();
      App.confirm(confirmEl.getAttribute("data-confirm"), confirmOptionsFrom(confirmEl)).then((ok) => {
        if (!ok) return;
        confirmEl.dataset.confirmed = "1";
        confirmEl.click();
        delete confirmEl.dataset.confirmed;
      });
    }
  });

  // data-confirm on forms (non-htmx submit)
  doc.addEventListener("submit", (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm") || form.dataset.confirmed) return;
    if (form.hasAttribute("hx-post") || form.hasAttribute("hx-put") || form.hasAttribute("hx-delete")) return;
    e.preventDefault();
    const submitter = e.submitter;
    App.confirm(form.getAttribute("data-confirm"), confirmOptionsFrom(form)).then((ok) => {
      if (!ok) return;
      form.dataset.confirmed = "1";
      if (form.requestSubmit) form.requestSubmit(submitter || undefined);
      else form.submit();
      delete form.dataset.confirmed;
    });
  });

  doc.addEventListener("keydown", (e) => {
    const top = topLayer();

    if (e.key === "Escape" && top) {
      if (e.defaultPrevented) return;
      e.preventDefault();
      if (top === "confirm-root" && confirmResolve) confirmResolve(false);
      else closeLayer(top);
      return;
    }

    // Focus trap in the topmost layer
    if (e.key === "Tab" && top) {
      const dialog = dialogOf(layerRoot(top));
      if (!dialog) return;
      const items = focusables(dialog);
      if (!items.length) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (!dialog.contains(doc.activeElement)) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && doc.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && doc.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
      return;
    }

    // Global shortcuts (not while typing, not with modifiers, not with a layer open)
    if (top || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented || isTyping(e.target)) return;
    if (e.key === "n" || e.key === "N") {
      const btn = doc.getElementById("btn-new-post");
      if (btn) {
        e.preventDefault();
        btn.click();
      }
    }
  });

  function syncThemeButtons() {
    const pref = App.theme.get();
    $$("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-set") === pref)));
  }

  // ------------------------------------------------------------------ Alpine components
  doc.addEventListener("alpine:init", () => {
    const Alpine = window.Alpine;

    // Menu button + popup. Markup: <div x-data="dropdown" @keydown="onKeydown($event)" @click.outside="close()">
    //   <button x-ref="button" @click="toggle()" :aria-expanded="open">…</button>
    //   <div x-ref="menu" x-show="open" x-cloak role="menu">…</div></div>
    Alpine.data("dropdown", () => ({
      open: false,
      toggle() {
        this.open ? this.close() : this.show();
      },
      show() {
        this.open = true;
        this.$dispatch("dropdown-open");
      },
      close(focusButton) {
        if (!this.open) return;
        this.open = false;
        if (focusButton && this.$refs.button) this.$refs.button.focus();
      },
      items() {
        const menu = this.$refs.menu;
        return menu ? $$('a[href], button:not([disabled]), [role="menuitem"], [role="menuitemradio"]', menu).filter((el) => el.offsetParent !== null) : [];
      },
      onKeydown(e) {
        if (e.key === "Escape" && this.open) {
          e.preventDefault();
          e.stopPropagation();
          this.close(true);
          return;
        }
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        if (isTyping(e.target) && e.target.tagName !== "BUTTON") return;
        e.preventDefault();
        if (!this.open) this.show();
        this.$nextTick(() => {
          const list = this.items();
          if (!list.length) return;
          const idx = list.indexOf(doc.activeElement);
          const next = e.key === "ArrowDown" ? (idx + 1) % list.length : (idx - 1 + list.length) % list.length;
          list[idx === -1 ? 0 : next].focus();
        });
      },
    }));

    // Client-side tabs: <div x-data="tabs('comun')"> … <button role="tab" @click="select('comun')" :aria-selected="is('comun')">
    Alpine.data("tabs", (initial) => ({
      tab: initial || null,
      select(key) {
        this.tab = key;
        this.$dispatch("tab-change", { tab: key });
      },
      is(key) {
        return this.tab === key;
      },
      onKeydown(e) {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        const list = $$('[role="tab"]', e.currentTarget);
        const idx = list.indexOf(doc.activeElement);
        if (idx === -1) return;
        e.preventDefault();
        const next = list[(idx + (e.key === "ArrowRight" ? 1 : -1) + list.length) % list.length];
        next.focus();
        next.click();
      },
    }));

    Alpine.data("themePicker", () => ({
      pref: App.theme.get(),
      set(p) {
        App.theme.set(p);
        this.pref = App.theme.get();
      },
    }));
  });

  // Spanish number formatting and token colours for Chart.js (if the page loaded it).
  App.applyChartDefaults = function () {
    const Chart = window.Chart;
    if (!Chart || !Chart.defaults) return;
    const t = App.chartTheme();
    Chart.defaults.locale = "es-ES";
    Chart.defaults.color = t.text;
    Chart.defaults.borderColor = t.grid;
    if (t.font) Chart.defaults.font.family = t.font;
  };
  window.addEventListener("themechange", () => App.applyChartDefaults());

  // Unread comments badge on the "Comentarios" nav item (GET /api/comentarios/resumen; refreshed when the
  // inbox changes something). Background request: never navigates, failures are silent.
  App.refreshInboxBadge = function () {
    const badge = doc.querySelector("[data-inbox-badge]");
    if (!badge || !window.fetch) return;
    fetch("/api/comentarios/resumen", { headers: { Accept: "application/json" }, credentials: "same-origin", redirect: "manual" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const n = data && Number(data.no_leidos);
        if (n > 0) {
          badge.textContent = n > 99 ? "99+" : String(n);
          badge.setAttribute("aria-label", n + (n === 1 ? " comentario sin leer" : " comentarios sin leer"));
          badge.hidden = false;
        } else {
          badge.hidden = true;
        }
      })
      .catch(() => {});
  };
  doc.addEventListener("inbox:changed", () => App.refreshInboxBadge());

  // ------------------------------------------------------------------ init
  function init() {
    App.applyChartDefaults();
    // Server flash messages (rendered as JSON by base.html)
    const flashEl = doc.getElementById("flash-data");
    if (flashEl) {
      try {
        JSON.parse(flashEl.textContent || "[]").forEach((f) => App.toast(f.message, f.kind || "info"));
      } catch (e) {
        /* ignore */
      }
    }
    // Content rendered straight into a layer root (full page load) opens it.
    ["drawer-root", "modal-root"].forEach((id) => {
      if (layerOpen(id)) activateLayer(id);
    });
    readNotifyPayload(doc.getElementById("notif-badge"));
    App.notify.syncUI();
    syncThemeButtons();
    // Narrow windows: the main nav scrolls horizontally; keep the current section visible.
    const nav = doc.querySelector(".mainnav");
    const current = nav && nav.querySelector(".is-active");
    if (current && nav.scrollWidth > nav.clientWidth) {
      const offset = current.getBoundingClientRect().left - nav.getBoundingClientRect().left;
      nav.scrollLeft += offset - (nav.clientWidth - current.offsetWidth) / 2;
    }
    App.refreshTimes();
    setInterval(() => App.refreshTimes(), 60000);
    setTimeout(() => App.refreshInboxBadge(), 400);
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();
