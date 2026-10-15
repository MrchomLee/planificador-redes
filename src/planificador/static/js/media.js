/* Planificador — media library (Biblioteca) + media picker.
 *
 * window.MediaLib:
 *   MediaLib.initLibrary(root)      /biblioteca page (auto on DOMContentLoaded when [data-medialib] exists)
 *   MediaLib.initPicker(root)       picker modal fragment (GET /biblioteca/selector); called by the fragment
 *   MediaLib.upload(files, opts)    upload with per-file progress (XHR, one request per file, 2 in parallel)
 *
 * Picker contract: on "Añadir" → window.dispatchEvent(new CustomEvent("media:selected",
 *   {detail: {items: [{id, kind, name, thumb_url, url, width, height, duration_s}]}})) and the modal closes.
 * Server events (HX-Trigger): "media:deleted" {id}, "media:changed" {id, name}.
 */
(function () {
  "use strict";
  if (window.MediaLib) return;

  const doc = document;
  const PARALLEL = 2;
  const DEFAULT_MAX = 4 * 1024 * 1024 * 1024;
  const ALLOWED_EXT = /\.(jpe?g|png|webp|gif|heic|heif|mp4|mov|m4v|webm)$/i;
  const $ = (sel, root) => (root || doc).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || doc).querySelectorAll(sel));
  const App = window.App || {};
  const esc = (s) => (App.escape ? App.escape(s) : String(s == null ? "" : s));
  const icon = (name) => (App.spriteIcon ? App.spriteIcon(name, 16) : "");
  const csrf = () => (App.csrfToken ? App.csrfToken() : ($('meta[name="csrf-token"]') || {}).content || "");
  const toast = (msg, kind) => (App.toast ? App.toast(msg, kind) : null);

  function fmtSize(n) {
    if (!n && n !== 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    const digits = i === 0 || v >= 100 ? 0 : 1;
    return v.toFixed(digits).replace(".", ",").replace(/,0$/, "") + " " + units[i];
  }

  function hasFiles(e) {
    const types = e.dataTransfer && e.dataTransfer.types;
    return !!types && Array.from(types).indexOf("Files") !== -1;
  }

  // ------------------------------------------------------------------ upload queue
  /** Uploads files one request each (field "files"), PARALLEL at a time, rendering rows in `tray`.
   *  opts: {url, maxBytes, tray, onUploaded(item), onFailed(row), onDone({ok, failed, items})} */
  function Uploader(opts) {
    this.opts = opts;
    this.queue = [];
    this.active = 0;
    this.total = 0;
    this.finished = 0;
    this.ok = 0;
    this.failed = 0;
    this.items = [];
    this.loaded = {};
    this.sizes = {};
  }

  Uploader.prototype.ensureTray = function () {
    const tray = this.opts.tray;
    if (!tray) return null;
    if (!$(".upload-tray__list", tray)) {
      tray.innerHTML =
        '<div class="upload-tray__head"><span class="upload-tray__title"></span>' +
        '<div class="progress" aria-hidden="true"><span style="width:0%"></span></div>' +
        '<button type="button" class="icon-btn icon-btn--sm" data-tray-close aria-label="Ocultar subidas" title="Ocultar">' +
        (App.icon ? App.icon("x") : "×") + "</button></div>" +
        '<ul class="upload-tray__list"></ul>';
      $("[data-tray-close]", tray).addEventListener("click", () => {
        tray.hidden = true;
        tray.innerHTML = "";
      });
    }
    tray.hidden = false;
    return $(".upload-tray__list", tray);
  };

  Uploader.prototype.updateHead = function () {
    const tray = this.opts.tray;
    if (!tray) return;
    const title = $(".upload-tray__title", tray);
    const bar = $(".upload-tray__head .progress > span", tray);
    let sent = 0;
    let size = 0;
    Object.keys(this.sizes).forEach((k) => {
      size += this.sizes[k];
      sent += Math.min(this.loaded[k] || 0, this.sizes[k]);
    });
    const pct = size ? Math.round((sent / size) * 100) : 100;
    if (bar) bar.style.width = pct + "%";
    if (!title) return;
    if (this.finished < this.total) {
      title.innerHTML = "<strong>Subiendo " + (this.finished + 1) + " de " + this.total + "</strong> · " + pct + " %";
    } else {
      const parts = [];
      if (this.ok) parts.push(this.ok === 1 ? "1 archivo subido" : this.ok + " archivos subidos");
      if (this.failed) parts.push(this.failed === 1 ? "1 con errores" : this.failed + " con errores");
      title.innerHTML = "<strong>" + esc(parts.join(" · ") || "Listo") + "</strong>";
    }
  };

  Uploader.prototype.add = function (fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const list = this.ensureTray();
    const max = Number(this.opts.maxBytes) || DEFAULT_MAX;
    files.forEach((file) => {
      const key = String(++this.total);
      const row = doc.createElement("li");
      row.className = "upload-row is-waiting";
      row.innerHTML =
        '<span class="upload-row__icon">' + icon(file.type.indexOf("video") === 0 ? "video" : "image") + "</span>" +
        '<div><div class="upload-row__name"></div><div class="progress"><span style="width:0%"></span></div>' +
        '<div class="upload-row__msg">En cola…</div></div><span class="upload-row__size"></span>';
      $(".upload-row__name", row).textContent = file.name;
      $(".upload-row__size", row).textContent = fmtSize(file.size);
      if (list) list.appendChild(row);
      this.sizes[key] = file.size || 1;
      const task = { file, row, key };
      if (file.size > max) {
        this.fail(task, "Supera el máximo de " + fmtSize(max) + ".");
      } else if (!ALLOWED_EXT.test(file.name) && !/^(image|video)\//.test(file.type)) {
        this.fail(task, "Tipo de archivo no admitido (JPEG, PNG, WebP, GIF, HEIC, MP4, MOV, M4V o WebM).");
      } else {
        this.queue.push(task);
      }
    });
    this.updateHead();
    this.pump();
  };

  Uploader.prototype.pump = function () {
    while (this.active < PARALLEL && this.queue.length) this.start(this.queue.shift());
  };

  Uploader.prototype.start = function (task) {
    this.active++;
    const row = task.row;
    row.classList.remove("is-waiting");
    $(".upload-row__msg", row).textContent = "Subiendo…";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", this.opts.url || "/biblioteca/subir");
    xhr.setRequestHeader("X-CSRF-Token", csrf());
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      this.loaded[task.key] = e.loaded;
      const pct = Math.round((e.loaded / e.total) * 100);
      $(".progress > span", row).style.width = pct + "%";
      $(".upload-row__msg", row).textContent = pct < 100 ? "Subiendo… " + pct + " %" : "Procesando…";
      this.updateHead();
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (e) {
        data = null;
      }
      const result = Array.isArray(data) ? data[0] : null;
      if (result && result.ok) this.succeed(task, result);
      else {
        const msg =
          (result && result.error) ||
          (data && data.detail) ||
          (App.errorMessage ? App.errorMessage(xhr.status, xhr.getResponseHeader("X-Error-Message"), xhr.getResponseHeader("Content-Type"), xhr.responseText) : "Error " + xhr.status);
        this.fail(task, msg);
      }
    };
    xhr.onerror = () => this.fail(task, "No se ha podido conectar con la aplicación.");
    const form = new FormData();
    form.append("files", task.file, task.file.name);
    xhr.send(form);
  };

  Uploader.prototype.succeed = function (task, item) {
    this.active = Math.max(0, this.active - 1);
    this.finished++;
    this.ok++;
    this.loaded[task.key] = this.sizes[task.key];
    this.items.push(item);
    const row = task.row;
    row.classList.add("is-done");
    $(".upload-row__icon", row).innerHTML = icon("circle-check");
    $(".progress > span", row).style.width = "100%";
    $(".upload-row__msg", row).textContent = "Subido";
    if (this.opts.onUploaded) this.opts.onUploaded(item);
    setTimeout(() => row.remove(), 4000);
    this.next();
  };

  Uploader.prototype.fail = function (task, message) {
    if (task.row.classList.contains("is-done")) return;
    const wasActive = !task.row.classList.contains("is-waiting");
    if (wasActive) this.active = Math.max(0, this.active - 1);
    this.finished++;
    this.failed++;
    this.loaded[task.key] = this.sizes[task.key];
    task.row.classList.remove("is-waiting");
    task.row.classList.add("is-error");
    $(".upload-row__icon", task.row).innerHTML = icon("circle-alert");
    $(".upload-row__msg", task.row).textContent = message;
    if (this.opts.onFailed) this.opts.onFailed(task.row, message);
    this.next();
  };

  Uploader.prototype.next = function () {
    this.updateHead();
    if (this.finished >= this.total && !this.active && !this.queue.length) {
      const summary = { ok: this.ok, failed: this.failed, items: this.items.slice() };
      if (this.opts.onDone) this.opts.onDone(summary);
      const tray = this.opts.tray;
      if (tray && !this.failed) {
        setTimeout(() => {
          if (this.finished >= this.total && !this.failed) {
            tray.hidden = true;
            tray.innerHTML = "";
          }
        }, 4500);
      }
      this.total = this.finished = this.ok = this.failed = 0;
      this.items = [];
      this.loaded = {};
      this.sizes = {};
    } else {
      this.pump();
    }
  };

  // ------------------------------------------------------------------ drag & drop helper
  function bindDrop(target, overlay, onFiles, zone) {
    let depth = 0;
    const show = (on) => {
      if (overlay) overlay.hidden = !on;
      if (zone) zone.classList.toggle("is-over", on);
    };
    target.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth++;
      show(true);
    });
    target.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    });
    target.addEventListener("dragleave", (e) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) show(false);
    });
    target.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      show(false);
      onFiles(e.dataTransfer.files);
    });
  }

  // Broken thumbnails → placeholder icon; unplayable preview videos (e.g. HEVC) → poster + note.
  // Capture phase: error events do not bubble.
  function videoFailed(video) {
    const box = video.closest(".media-detail__preview");
    const fallback = box && $("[data-preview-fallback]", box);
    if (!fallback) return;
    video.hidden = true;
    fallback.hidden = false;
    box.classList.remove("is-video");
  }
  doc.addEventListener(
    "error",
    (e) => {
      const el = e.target;
      if (!el || !el.closest) return;
      if (el.tagName === "IMG" && el.closest(".media-card__thumb")) el.classList.add("is-broken");
      if (el.tagName === "VIDEO" && el.hasAttribute("data-preview-video")) videoFailed(el);
      if (el.tagName === "SOURCE" && el.parentElement && el.parentElement.hasAttribute("data-preview-video")) videoFailed(el.parentElement);
    },
    true
  );
  // Some browsers report an unsupported codec only as "no video track": detect a 0×0 video after metadata.
  doc.addEventListener(
    "loadedmetadata",
    (e) => {
      const el = e.target;
      if (el && el.tagName === "VIDEO" && el.hasAttribute("data-preview-video") && !el.videoWidth) videoFailed(el);
    },
    true
  );

  // ------------------------------------------------------------------ library page
  function initLibrary(root) {
    if (!root || root.dataset.mlInit) return;
    root.dataset.mlInit = "1";
    const layout = $("#media-layout", root);
    const detail = $("#media-detail", root);
    const input = $("[data-upload-input]", root);
    const form = $(".medialib__toolbar", root);

    function refresh() {
      if (!window.htmx || !form) return;
      const params = new URLSearchParams(new FormData(form));
      return window.htmx.ajax("GET", "/biblioteca?" + params.toString(), { target: "#media-results", swap: "innerHTML" });
    }

    const uploader = new Uploader({
      url: root.dataset.uploadUrl,
      maxBytes: root.dataset.maxBytes,
      tray: $("[data-upload-tray]", root),
      onDone: (summary) => {
        if (summary.ok) {
          toast(summary.ok === 1 ? "Archivo añadido a la biblioteca" : summary.ok + " archivos añadidos a la biblioteca", "success");
          refresh();
        }
      },
    });

    root.addEventListener("click", (e) => {
      if (e.target.closest("[data-upload-trigger]")) {
        e.preventDefault();
        if (input) input.click();
      }
      if (e.target.closest("[data-detail-close]")) {
        e.preventDefault();
        closeDetail();
      }
    });
    if (input) {
      input.addEventListener("change", () => {
        uploader.add(input.files);
        input.value = "";
      });
    }
    bindDrop(doc, $("[data-drop-overlay]", root), (files) => uploader.add(files));

    function markSelected(id) {
      $$(".media-card.is-selected", root).forEach((c) => c.classList.remove("is-selected"));
      if (id) {
        const card = $('.media-card[data-media-id="' + id + '"]', root);
        if (card) card.classList.add("is-selected");
      }
    }
    function syncDetail() {
      const panel = detail && $("[data-media-detail]", detail);
      if (layout) layout.classList.toggle("has-detail", !!panel);
      markSelected(panel ? panel.dataset.mediaId : null);
    }
    function closeDetail() {
      if (detail) detail.innerHTML = "";
      syncDetail();
      const url = new URL(window.location.href);
      if (url.searchParams.has("medio")) {
        url.searchParams.delete("medio");
        window.history.replaceState(window.history.state, "", url.toString());
      }
    }

    doc.addEventListener("htmx:afterSwap", (e) => {
      const target = e.detail && e.detail.target;
      if (!target) return;
      if (target === detail) {
        syncDetail();
        const name = $("[data-rename-input]", detail);
        if (name) name.blur();
      } else if (target.id === "media-results" || target.id === "media-more" || target.closest("#media-results")) {
        syncDetail();
      }
    });
    doc.addEventListener("media:deleted", (e) => {
      const id = e.detail && e.detail.id;
      const card = id && $('.media-card[data-media-id="' + id + '"]', root);
      closeDetail();
      if (card) {
        card.classList.add("is-removing");
        setTimeout(refresh, 160);
      } else refresh();
    });
    doc.addEventListener("media:changed", (e) => {
      const d = e.detail || {};
      const card = d.id && $('.media-card[data-media-id="' + d.id + '"]', root);
      if (card && d.name) {
        const name = $(".media-card__name", card);
        if (name) name.textContent = d.name;
        const open = $(".media-card__open", card);
        if (open) open.setAttribute("aria-label", "Ver detalles de " + d.name);
      }
    });
    // Rename: submit on Enter (form default) and on blur when changed; Esc restores.
    root.addEventListener(
      "blur",
      (e) => {
        const el = e.target;
        if (!el.matches || !el.matches("[data-rename-input]")) return;
        if (el.value.trim() && el.value !== el.defaultValue && el.form) {
          if (el.form.requestSubmit) el.form.requestSubmit();
        } else el.value = el.defaultValue;
      },
      true
    );
    root.addEventListener("keydown", (e) => {
      if (e.target.matches && e.target.matches("[data-rename-input]") && e.key === "Escape") {
        e.target.value = e.target.defaultValue;
        e.target.blur();
        e.stopPropagation();
        e.preventDefault();
      }
    });
    doc.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (App.layers && App.layers.top && App.layers.top()) return;
      if (detail && detail.firstElementChild) closeDetail();
    });
    syncDetail();
  }

  // ------------------------------------------------------------------ picker
  function pickItem(data) {
    return {
      id: data.id,
      kind: data.kind,
      name: data.name,
      thumb_url: data.thumb_url,
      url: data.url,
      width: data.width,
      height: data.height,
      duration_s: data.duration_s,
    };
  }

  function initPicker(modal) {
    const root = modal && (modal.matches("[data-media-picker]") ? modal : $("[data-media-picker]", modal));
    if (!root || root.dataset.mlInit) return;
    root.dataset.mlInit = "1";
    const dialog = root.closest(".modal") || modal;
    const multiple = root.dataset.multiple === "1";
    const kind = root.dataset.kind || "any";
    const selected = new Map();
    const confirmBtn = $("[data-picker-confirm]", dialog);
    const countEl = $("[data-picker-count]", dialog);
    const input = $("[data-upload-input]", root);
    const form = $(".picker__toolbar", root);
    const results = $("#picker-results", root);

    function render() {
      $$(".media-card__input", root).forEach((el) => {
        el.checked = selected.has(Number(el.value));
      });
      const n = selected.size;
      if (countEl) {
        countEl.innerHTML = n
          ? "<strong>" + n + "</strong> " + (n === 1 ? "archivo seleccionado" : "archivos seleccionados")
          : "Ningún archivo seleccionado";
      }
      if (confirmBtn) confirmBtn.disabled = n === 0;
    }

    function select(item, on) {
      if (!item) return;
      const id = Number(item.id);
      if (on) {
        if (!multiple) selected.clear();
        selected.set(id, pickItem(item));
      } else selected.delete(id);
      render();
    }

    // The picker may live directly in a layer root (#modal-root) or nested inside another dialog (e.g. the
    // composer loads it into its own container): then it must close itself without closing its host.
    const backdrop = root.closest(".modal-backdrop");
    const layer = backdrop && backdrop.parentElement;
    const ownsLayer = !!(layer && layer.classList.contains("layer-root") && layer.firstElementChild === backdrop);

    function closePicker() {
      if (ownsLayer && App.layers) App.layers.close(layer.id, { force: true });
      else if (backdrop) backdrop.remove();
    }

    if (!ownsLayer && backdrop) {
      const box = $(".modal", backdrop);
      if (box) requestAnimationFrame(() => box.focus({ preventScroll: true }));
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop || e.target.closest("[data-close]")) {
          e.preventDefault();
          e.stopPropagation();
          closePicker();
        }
      });
      backdrop.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          closePicker();
        }
      });
    }

    function confirm() {
      if (!selected.size) return;
      const items = Array.from(selected.values());
      window.dispatchEvent(new CustomEvent("media:selected", { detail: { items: items } }));
      closePicker();
    }

    function refresh() {
      if (!window.htmx || !form) return;
      const params = new URLSearchParams(new FormData(form));
      return window.htmx.ajax("GET", "/biblioteca/selector?" + params.toString(), { target: "#picker-results", swap: "innerHTML" });
    }

    root.addEventListener("change", (e) => {
      const el = e.target;
      if (!el.classList || !el.classList.contains("media-card__input")) return;
      let data = {};
      try {
        data = JSON.parse(el.getAttribute("data-item") || "{}");
      } catch (err) {
        data = { id: Number(el.value) };
      }
      select(data, el.checked);
    });
    root.addEventListener("dblclick", (e) => {
      const card = e.target.closest(".media-card--pick");
      if (!card) return;
      const el = $(".media-card__input", card);
      if (!el) return;
      if (!el.checked) {
        el.checked = true;
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (!multiple) confirm();
    });
    if (confirmBtn) confirmBtn.addEventListener("click", confirm);

    const uploader = new Uploader({
      url: root.dataset.uploadUrl,
      maxBytes: root.dataset.maxBytes,
      tray: $("[data-upload-tray]", root),
      onUploaded: (item) => {
        if (kind === "any" || item.kind === kind) select(item, true);
        else toast("«" + item.name + "» se ha guardado, pero aquí solo puedes elegir " + (kind === "image" ? "fotos" : "vídeos") + ".", "warning");
      },
      onDone: (summary) => {
        if (summary.ok) refresh();
      },
    });
    root.addEventListener("click", (e) => {
      if (e.target.closest("[data-upload-trigger]")) {
        e.preventDefault();
        if (input) input.click();
      }
    });
    if (input) {
      input.addEventListener("change", () => {
        uploader.add(multiple ? input.files : Array.from(input.files).slice(0, 1));
        input.value = "";
      });
    }
    if (results) bindDrop(root, null, (files) => uploader.add(multiple ? files : Array.from(files).slice(0, 1)), results);

    // Results swapped (search, filter, "Cargar más", refresh) → restore checks.
    const onSwap = (e) => {
      if (!root.isConnected) {
        doc.removeEventListener("htmx:afterSwap", onSwap);
        return;
      }
      const target = e.detail && e.detail.target;
      if (target && root.contains(target)) render();
    };
    doc.addEventListener("htmx:afterSwap", onSwap);
    render();
  }

  function autoInit() {
    const lib = $("[data-medialib]");
    if (lib) initLibrary(lib);
    $$("[data-media-picker]").forEach((p) => initPicker(p));
  }

  window.MediaLib = {
    initLibrary: initLibrary,
    initPicker: initPicker,
    Uploader: Uploader,
    upload: function (files, opts) {
      const up = new Uploader(opts || {});
      up.add(files);
      return up;
    },
    pickItem: pickItem,
  };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", autoInit);
  else autoInit();
})();
