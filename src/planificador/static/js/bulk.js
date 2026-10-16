/* Planificador — "Carga masiva" (routes/bulk.py, bulk.py).
 *
 * Alpine component `bulkPage`, mounted by templates/bulk/index.html. State comes from
 * <script type="application/json" data-bulk-state>. The persisted model is `batch` (same schema as
 * bulk.sanitize_batch): it is autosaved (debounced POST /api/carga-masiva/lote) on every change, so closing the
 * tab keeps everything. Validation (POST /api/carga-masiva/comprobar, debounced) runs postsvc.validate_post on
 * the rows that changed (all of them when a common setting changes). "Programar todo" posts the batch with its
 * token: the server creates each post once, whatever the number of clicks.
 *
 *   batch.accounts[key]  {selected, format, auto_publish, options}  (key = account id, or "demo")
 *   batch.rows[]         {id, media_id, text, hashtags[], yt_title, date, time, networks{key: false}}
 *
 * The TikTok music-usage consent (`consent`) is never stored: it is ticked again before scheduling.
 *
 * The batch carries the `brand_id` the page was opened for: if the active brand changes in another tab the
 * server answers 409 `brand_changed` and the page stops (inert, "No se guarda") until it is reloaded. The same
 * happens when the batch was scheduled elsewhere (409 `submitted`). While "Programar todo" runs the steps are
 * inert and nothing is autosaved.
 */
(function () {
  "use strict";

  const doc = document;
  const App = window.App || (window.App = {});

  const VERTICAL_DEFAULTS = { instagram: "reel", facebook: "reel", tiktok: "video", youtube: "short" };
  const HORIZONTAL_DEFAULTS = { instagram: "reel", facebook: "post", tiktok: "video", youtube: "video" };
  const NETWORK_ORDER = ["facebook", "instagram", "tiktok", "youtube", "demo"];
  const UPLOAD_PARALLEL = 2;
  const SAVE_DELAY = 800;
  const CHECK_DELAY = 600;
  const VIDEO_EXT = /\.(mp4|mov|m4v|webm)$/i;
  const WEEKDAYS = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
  const MONTHS_SHORT = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"];

  const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  const pad = (n) => String(n).padStart(2, "0");
  const toast = (msg, kind) => (App.toast ? App.toast(msg, kind) : window.alert(msg));
  const confirmDialog = (msg, opts) => (App.confirm ? App.confirm(msg, opts) : Promise.resolve(window.confirm(msg)));
  const uid = () => "r" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const FIELD_SIZING = !!(window.CSS && CSS.supports && CSS.supports("field-sizing", "content"));

  // ------------------------------------------------------------------ text helpers (mirror bulk.py)
  /** Length in the unit each network counts (Python len = code points; TikTok UTF-16; YouTube bytes). */
  function textLength(unit, text) {
    const t = text || "";
    if (unit === "utf16") return t.length;
    if (unit === "bytes") return new TextEncoder().encode(t).length;
    let n = 0;
    for (const _ of t) n++; // eslint-disable-line no-unused-vars
    return n;
  }
  const TAG_IN_TEXT = /(^|[^\p{L}\p{M}\p{N}_&])#([\p{L}\p{M}\p{N}_]+)/gu;
  const TAG_INVALID = /[^\p{L}\p{M}\p{N}_]/gu;
  function tagsInText(text) {
    const out = [];
    for (const m of (text || "").matchAll(TAG_IN_TEXT)) out.push(m[2]);
    return out;
  }
  /** bulk.normalize_hashtags: no "#", word characters only, unique (case-insensitive). */
  function normalizeTags(value, limit) {
    const items = Array.isArray(value) ? value : [value];
    const out = [];
    const seen = new Set();
    for (const item of items) {
      if (item == null) continue;
      for (const part of String(item).split(/[\s,;]+/)) {
        const tag = part.replace(/^#+/, "").normalize("NFC").replace(TAG_INVALID, "").slice(0, 80);
        if (!tag || seen.has(tag.toLowerCase())) continue;
        seen.add(tag.toLowerCase());
        out.push(tag);
        if (out.length >= (limit || 60)) return out;
      }
    }
    return out;
  }
  /** bulk.compose_caption: text + row hashtags + common hashtags (no repeats); on the same line after a
   * one-line text, below a blank line after a text of several lines. */
  function composeCaption(text, tags, common) {
    const base = (text || "").replace(/\r\n?/g, "\n").replace(/\s+$/, "");
    const present = new Set(tagsInText(base).map((t) => t.toLowerCase()));
    const extra = [];
    for (const tag of normalizeTags(tags).concat(normalizeTags(common))) {
      if (present.has(tag.toLowerCase())) continue;
      present.add(tag.toLowerCase());
      extra.push(tag);
    }
    if (!extra.length) return base;
    const line = extra.map((t) => "#" + t).join(" ");
    if (!base.trim()) return line;
    return base + (base.includes("\n") ? "\n\n" : " ") + line;
  }
  function countHashtags(text) {
    return tagsInText(text).length;
  }
  function cleanFilename(name) {
    let stem = String(name || "");
    const dot = stem.lastIndexOf(".");
    if (dot > 0) stem = stem.slice(0, dot);
    return stem.replace(/[_\-.]+/g, " ").replace(/\s+/g, " ").trim();
  }
  function firstLine(text) {
    for (const line of (text || "").split("\n")) {
      const clean = line.replace(/[<>]/g, "").trim();
      if (clean) return clean;
    }
    return "";
  }
  const SEPARATOR_LINE = /^[ \t]*-{3,}[ \t]*$/m;
  const BLANK_LINES = /\n(?:[ \t]*\n)+/;
  const ONLY_TAGS = /^[#@][\p{L}\p{M}\p{N}_.]+(?:[\s,;]+[#@][\p{L}\p{M}\p{N}_.]+)*[\s,;]*$/u;
  /** How pasted texts are separated: the chosen mode, or for "auto" lines of "---" if there are any, else
   * blank lines, else one text per line. */
  function pasteMethod(text, mode) {
    if (mode === "sep" || mode === "blank" || mode === "line") return mode;
    const t = (text || "").replace(/\r\n?/g, "\n");
    if (SEPARATOR_LINE.test(t)) return "sep";
    return BLANK_LINES.test(t.trim()) ? "blank" : "line";
  }
  /** "Pegar textos" → one text per video. "sep": texts separated by a line with only "---" (they may have
   * blank lines inside); "blank": by a blank line; "line": one per line. In "auto" mode with blank lines, a
   * block of only hashtags/mentions belongs to the text before it ("Texto" + blank line + "#tags"). */
  function splitPasted(text, mode) {
    const t = (text || "").replace(/\r\n?/g, "\n");
    const method = pasteMethod(t, mode);
    let parts;
    if (method === "sep") parts = t.split(new RegExp(SEPARATOR_LINE.source, "m"));
    else if (method === "blank") parts = t.split(BLANK_LINES);
    else parts = t.split("\n");
    parts = parts.map((p) => p.trim()).filter(Boolean);
    if (mode === "auto" && method === "blank") {
      const joined = [];
      parts.forEach((p) => {
        if (joined.length && ONLY_TAGS.test(p)) joined[joined.length - 1] += "\n\n" + p;
        else joined.push(p);
      });
      parts = joined;
    }
    return parts;
  }
  function duration(seconds) {
    if (seconds == null || isNaN(seconds)) return "";
    const s = Math.round(seconds);
    const m = Math.floor(s / 60);
    return m >= 60 ? Math.floor(m / 60) + ":" + pad(m % 60) + ":" + pad(s % 60) : m + ":" + pad(s % 60);
  }
  function fileSize(bytes) {
    const n = Number(bytes || 0);
    if (n < 1024 * 1024) return Math.max(1, Math.round(n / 1024)) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1).replace(".", ",") + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(2).replace(".", ",") + " GB";
  }
  function shortDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!m) return "";
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return WEEKDAYS[d.getUTCDay()] + " " + d.getUTCDate() + " " + MONTHS_SHORT[d.getUTCMonth()];
  }
  /** "vertical" | "horizontal" | "square" | "" from a library item's (rotation-corrected) size. */
  function orientationOf(m) {
    if (!m) return "";
    if (m.orientation) return m.orientation;
    if (!m.width || !m.height) return "";
    return m.height > m.width ? "vertical" : m.width > m.height ? "horizontal" : "square";
  }
  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  function component() {
    // Non-reactive bookkeeping (kept out of Alpine's proxy).
    const priv = {
      lastSaved: "",
      lastCommon: "",
      rowSnaps: {},
      saveTimer: null,
      checkTimer: null,
      checkSeq: 0,
      dirtyRows: new Set(),
      checkAll: false,
      queue: [],
      active: 0,
      unload: null,
      stopped: false,
      stopCode: "",
      memo: null,
      sizeQueue: new Set(),
      sizeFrame: 0,
      formatChosen: new Set(),
      seq: 0,
      rowSeq: {},
    };
    return {
      ready: false,
      cfg: { urls: {}, limits: {}, legal: {}, brand: {}, privacy_labels: {} },
      accounts: [],
      acc: {},
      batch: { rows: [], accounts: {}, distribution: { slots: [] }, common_hashtags: [] },
      media: {},
      uploads: [],
      dropping: false,
      checks: {},
      report: null,
      checking: false,
      consent: false,
      saveState: "idle",
      savedAt: null,
      saveError: "",
      submitting: false,
      stopReason: "",
      result: null,
      lastResult: null,
      restored: false,
      interrupted: null,
      distributing: false,
      distNotes: [],
      distError: "",
      newSlot: "",
      creator: {},
      ytCats: {},
      notice: {},
      drafts: {},
      pickerPending: false,
      dragIndex: null,
      dragOver: null,
      paste: { open: false, text: "", mode: "auto", onlyEmpty: false },
      onlyIssues: false,

      // ---------------------------------------------------------------- lifecycle
      init() {
        const holder = this.$el.querySelector("script[data-bulk-state]");
        let s = {};
        try {
          s = JSON.parse((holder && holder.textContent) || "{}");
        } catch (e) {
          toast("No se ha podido abrir la carga masiva. Recarga la página.", "error");
        }
        this.cfg = Object.assign(this.cfg, s);
        this.accounts = (s.accounts || []).slice().sort((a, b) => NETWORK_ORDER.indexOf(a.network) - NETWORK_ORDER.indexOf(b.network));
        const acc = {};
        this.accounts.forEach((a) => (acc[a.key] = a));
        this.acc = acc;
        this.media = Object.assign({}, s.media_index || {});
        this.restored = !!s.restored;
        this.interrupted = s.interrupted || null;
        this.lastResult = s.last_result || null;
        this.batch = this.normalizeBatch(s.batch || {});
        priv.lastSaved = JSON.stringify(this.batch); // nothing to save until the user changes something
        this.snapshotRows();
        this.selectedKeys().forEach((k) => this.onSelected(k));
        priv.unload = (e) => {
          if (priv.stopped) return;
          if (JSON.stringify(this.batch) !== priv.lastSaved) {
            this.flushSave(true);
            if (this.saveState === "error") {
              e.preventDefault();
              e.returnValue = "";
            }
          }
        };
        window.addEventListener("beforeunload", priv.unload);
        this.$el.addEventListener("keydown", (e) => this.onKeydown(e));
        doc.addEventListener("layer:closed", () => (this.pickerPending = false));
        this.$nextTick(() => {
          this.ready = true;
          if (this.batch.rows.length) this.scheduleCheck(true);
          if (this.restored && this.batch.rows.length) {
            toast("Hemos recuperado tu carga masiva: " + plural(this.batch.rows.length, "vídeo", "vídeos") + ".", "info");
          }
        });
      },
      destroy() {
        if (priv.unload) window.removeEventListener("beforeunload", priv.unload);
        clearTimeout(priv.saveTimer);
        clearTimeout(priv.checkTimer);
      },

      /** Fill in what a stored/new batch lacks (one config per account, defaults). */
      normalizeBatch(b) {
        const batch = Object.assign(
          { step: 1, accounts: {}, common_hashtags: [], yt_title_source: "text", rows: [] },
          clone(b) || {}
        );
        if (!batch.brand_id) batch.brand_id = (this.cfg.brand && this.cfg.brand.id) || null;
        batch.distribution = Object.assign(
          { start_date: "", mode: "per_day", per_day: 2, every_days: 1, slots: (this.cfg.suggested_slots || ["12:00", "19:00"]).slice(), skip_weekends: false },
          batch.distribution || {}
        );
        if (!batch.distribution.start_date) batch.distribution.start_date = this.cfg.today || "";
        if (!Array.isArray(batch.distribution.slots)) batch.distribution.slots = [];
        const accounts = {};
        this.accounts.forEach((a) => {
          const stored = (batch.accounts || {})[a.key];
          accounts[a.key] = this.accountConf(a, stored);
        });
        if (this.accounts.length === 1 && !Object.values(batch.accounts || {}).some((c) => c && c.selected)) accounts[this.accounts[0].key].selected = true;
        batch.accounts = accounts;
        batch.rows = (batch.rows || []).map((r) =>
          Object.assign({ text: "", hashtags: [], yt_title: "", date: "", time: "", networks: {} }, r, {
            hashtags: Array.isArray(r.hashtags) ? r.hashtags : normalizeTags(r.hashtags),
            networks: r.networks && typeof r.networks === "object" ? r.networks : {},
          })
        );
        batch.step = Math.min(4, Math.max(1, Number(batch.step) || 1));
        return batch;
      },
      accountConf(a, stored) {
        const fmt = (stored && stored.format && this.formatSpec(a, stored.format) && !this.formatSpec(a, stored.format).disabled && stored.format) || this.defaultFormat(a);
        const spec = this.formatSpec(a, fmt) || {};
        const options = Object.assign(clone(spec.defaults || {}), stored ? clone(stored.options || {}) : {});
        delete options.consent;
        if (a.network === "tiktok") {
          if (!("commercial_content" in options)) options.commercial_content = !!(options.brand_organic_toggle || options.brand_content_toggle);
          const modes = this.modesOf(a);
          if (!options.mode) options.mode = "direct";
          if (modes.length && !modes.includes(options.mode)) options.mode = modes[0];
        }
        return {
          selected: !!(stored && stored.selected) && a.status !== "disconnected",
          format: fmt,
          auto_publish: stored && typeof stored.auto_publish === "boolean" ? stored.auto_publish : a.auto_publish_default !== false,
          options,
        };
      },

      // ---------------------------------------------------------------- change tracking
      /** x-effect: any change of the batch → autosave; rows/common settings changes → validation. */
      track() {
        const snap = JSON.stringify(this.batch);
        void this.consent;
        // While "Programar todo" runs the page is inert and the batch is about to be replaced: nothing to save
        // (an autosave would race the submit). This effect runs again when ``submitting`` changes.
        if (!this.ready || priv.stopped || this.submitting) return;
        if (snap !== priv.lastSaved) this.scheduleSave();
        const common = this.commonSnap();
        if (common !== priv.lastCommon) {
          priv.lastCommon = common;
          this.scheduleCheck(true);
        }
        const seen = {};
        this.batch.rows.forEach((r) => {
          const rs = JSON.stringify(r);
          seen[r.id] = rs;
          if (priv.rowSnaps[r.id] !== rs) {
            priv.dirtyRows.add(r.id);
            this.scheduleCheck(false);
          }
        });
        if (Object.keys(priv.rowSnaps).length !== this.batch.rows.length) this.scheduleCheck(false);
        priv.rowSnaps = seen;
      },
      commonSnap() {
        return JSON.stringify([this.batch.accounts, this.batch.common_hashtags, this.batch.yt_title_source, this.consent]);
      },
      snapshotRows() {
        priv.rowSnaps = {};
        this.batch.rows.forEach((r) => (priv.rowSnaps[r.id] = JSON.stringify(r)));
        priv.lastCommon = this.commonSnap();
      },

      // ---------------------------------------------------------------- autosave
      scheduleSave() {
        if (priv.stopped || this.submitting) return;
        this.saveState = "dirty";
        clearTimeout(priv.saveTimer);
        priv.saveTimer = setTimeout(() => this.save(), SAVE_DELAY);
      },
      async save() {
        clearTimeout(priv.saveTimer);
        if (priv.stopped) return true;
        const snap = JSON.stringify(this.batch);
        if (snap === priv.lastSaved) {
          if (this.saveState !== "error") this.saveState = this.savedAt ? "saved" : "idle";
          return true;
        }
        this.saveState = "saving";
        try {
          const res = await App.fetchJSON(this.cfg.urls.batch, { body: { batch: JSON.parse(snap) } });
          priv.lastSaved = snap;
          this.savedAt = new Date();
          this.saveError = "";
          this.saveState = JSON.stringify(this.batch) === snap ? "saved" : "dirty";
          if (this.saveState === "dirty") this.scheduleSave();
          return !!res;
        } catch (e) {
          if (this.conflict(e)) return false;
          this.saveState = "error";
          this.saveError = e.message || "No se ha podido guardar.";
          clearTimeout(priv.saveTimer);
          priv.saveTimer = setTimeout(() => this.save(), 8000);
          return false;
        }
      },
      /** On page exit: send the pending batch with keepalive (no response awaited). */
      flushSave() {
        try {
          fetch(this.cfg.urls.batch, {
            method: "POST",
            keepalive: true,
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": App.csrfToken ? App.csrfToken() : "" },
            body: JSON.stringify({ batch: this.batch }),
          });
        } catch (e) {
          /* the debounced save already ran or the browser refused: nothing else to do */
        }
      },
      /** 409 ``brand_changed`` (the active brand changed in another tab) or ``submitted`` (this batch was
       * scheduled elsewhere): stop autosaving and validating until the page is reloaded. */
      conflict(e) {
        const code = e && e.status === 409 && e.data ? e.data.code : "";
        if (code !== "brand_changed" && code !== "submitted") return false;
        const unsaved = JSON.stringify(this.batch) !== priv.lastSaved;
        this.stop(code, (e.message || "") + (unsaved ? " Los últimos cambios de esta pestaña no se han guardado." : ""));
        return true;
      },
      stop(code, reason) {
        priv.stopped = true;
        priv.stopCode = code;
        clearTimeout(priv.saveTimer);
        clearTimeout(priv.checkTimer);
        this.checking = false;
        this.stopReason = reason || "Esta pestaña ya no guarda los cambios. Recarga la página.";
        this.saveState = "stopped";
      },
      /** Autosave and validation back on (a new batch after scheduling or discarding). A brand change is only
       * fixed by reloading. */
      resume() {
        if (priv.stopCode === "brand_changed") return;
        priv.stopped = false;
        priv.stopCode = "";
        this.stopReason = "";
      },
      reload() {
        window.location.reload();
      },
      interruptedText() {
        const it = this.interrupted;
        if (!it) return "";
        let msg = "La aplicación se cerró mientras se guardaba esta carga masiva. ";
        msg += it.done ? plural(it.done, "vídeo ya se había guardado", "vídeos ya se habían guardado") + " (están en el calendario) y no se repetirá" + (it.done === 1 ? "" : "n") + ". " : "";
        const left = this.batch.rows.length;
        msg += left ? (left === 1 ? "Falta 1 vídeo: es el que ves aquí." : "Faltan " + left + " vídeos: son los que ves aquí.") : "Solo falta terminar.";
        return msg + " También puedes usar «Guardar todo como borradores».";
      },
      saveLabel() {
        if (this.saveState === "stopped") return "No se guarda";
        if (this.saveState === "saving") return "Guardando…";
        if (this.saveState === "dirty") return "Cambios sin guardar";
        if (this.saveState === "error") return "No se ha podido guardar";
        if (this.savedAt) {
          const d = this.savedAt;
          return "Guardado a las " + pad(d.getHours()) + ":" + pad(d.getMinutes());
        }
        return this.restored ? "Recuperado" : "Todo guardado";
      },

      // ---------------------------------------------------------------- validation
      scheduleCheck(all) {
        if (all) priv.checkAll = true;
        clearTimeout(priv.checkTimer);
        priv.checkTimer = setTimeout(() => this.check(), CHECK_DELAY);
      },
      async check() {
        clearTimeout(priv.checkTimer);
        if (priv.stopped) return;
        if (!this.batch.rows.length && !this.selectedKeys().length) {
          this.report = null;
          this.checks = {};
          priv.dirtyRows.clear();
          priv.checkAll = false;
          return;
        }
        const live = new Set(this.batch.rows.map((r) => r.id));
        // Only rows still in the list (a removed or replaced row may still be marked as changed).
        const dirty = Array.from(priv.dirtyRows).filter((id) => live.has(id));
        const all = priv.checkAll || dirty.length >= live.size;
        const ids = all ? null : dirty;
        priv.checkAll = false;
        priv.dirtyRows.clear();
        const seq = ++priv.checkSeq;
        this.checking = true;
        try {
          const body = { batch: clone(this.batch), tiktok_consent: this.consent };
          if (ids) body.rows = ids;
          const res = await App.fetchJSON(this.cfg.urls.check, { body });
          if (seq !== priv.checkSeq) return;
          const checks = all ? {} : Object.assign({}, this.checks);
          Object.keys(res.rows || {}).forEach((k) => (checks[k] = res.rows[k]));
          const now = new Set(this.batch.rows.map((r) => r.id));
          Object.keys(checks).forEach((k) => now.has(k) || delete checks[k]);
          this.checks = checks;
          this.report = res;
        } catch (e) {
          if (seq !== priv.checkSeq) return;
          if (this.conflict(e)) return;
          if (ids) ids.forEach((id) => priv.dirtyRows.add(id));
          if (all) priv.checkAll = true;
        } finally {
          if (seq === priv.checkSeq) this.checking = false;
        }
      },
      rowCheck(row) {
        return this.checks[row.id] || null;
      },
      /** Status of every row, computed once per check result and list of rows: the warnings every video has
       * (same network and code, shown once above the list), each row's own warnings and status, and the counts.
       * The templates ask for these per row (and the step tabs for all rows on every keystroke): computing
       * them on each call was quadratic in the number of videos. Reading ``checks`` and ``rows`` here keeps
       * every caller reactive to them; the work only happens when they change. */
      derived() {
        const checks = this.checks;
        const rows = this.batch.rows;
        const n = rows.length;
        const m = priv.memo;
        if (m && m.checks === checks && m.rows === rows && m.n === n) return m;
        const raw = (v) => (window.Alpine && window.Alpine.raw ? window.Alpine.raw(v) : v);
        const all = raw(checks) || {};
        const list = raw(rows).map((r) => raw(r));
        const key = (w) => (w.account_key || "") + "|" + w.code;
        let shared = [];
        if (n >= 2 && list.every((r) => all[r.id])) {
          let common = null;
          list.forEach((r) => {
            const keys = new Set(all[r.id].warnings.map(key));
            common = common === null ? keys : new Set([...common].filter((k) => keys.has(k)));
          });
          if (common && common.size) shared = all[list[0].id].warnings.filter((w) => common.has(key(w)));
        }
        const sharedKeys = new Set(shared.map(key));
        const byRow = {};
        let errors = 0;
        let contentErrors = 0;
        list.forEach((r) => {
          const c = all[r.id];
          if (!c) {
            byRow[r.id] = { status: "pending", own: [], errors: [] };
            return;
          }
          const own = c.warnings.filter((w) => !sharedKeys.has(key(w)));
          const status = c.errors.length ? "error" : own.length ? "warning" : "ok";
          if (status === "error") errors++;
          if (c.errors.some((e) => e.code !== "date_required")) contentErrors++;
          byRow[r.id] = { status, own, errors: c.errors };
        });
        priv.memo = { checks, rows, n, shared, byRow, errors, contentErrors };
        return priv.memo;
      },
      rowInfo(row) {
        const info = this.derived().byRow[row.id];
        if (info) return info;
        priv.memo = null; // rows changed in place (same count): recompute once
        return this.derived().byRow[row.id] || { status: "pending", own: [], errors: [] };
      },
      /** Warnings every video has (same network and code): shown once above the list, not in each card. */
      sharedWarnings() {
        return this.derived().shared;
      },
      ownWarnings(row) {
        return this.rowInfo(row).own;
      },
      rowStatus(row) {
        return this.rowInfo(row).status;
      },
      rowBadge(row) {
        const info = this.rowInfo(row);
        if (info.status === "pending") return "Comprobando…";
        const e = info.errors.length;
        const w = info.own.length;
        if (e) return e === 1 ? "1 error" : e + " errores";
        if (w) return w === 1 ? "1 aviso" : w + " avisos";
        return "Listo";
      },
      rowsWithErrors() {
        return this.derived().errors;
      },
      /** Rows with errors other than "no date yet" (step 3 deals with dates). */
      rowsWithContentErrors() {
        return this.derived().contentErrors;
      },
      commonErrors() {
        return (this.report && this.report.common && this.report.common.errors) || [];
      },
      commonWarnings() {
        return (this.report && this.report.common && this.report.common.warnings) || [];
      },
      caps() {
        return (this.report && this.report.caps) || [];
      },
      summary() {
        return (this.report && this.report.summary) || null;
      },
      canSchedule() {
        return (!!this.batch.rows.length || !!this.interrupted) && !!this.selectedKeys().length && !this.commonErrors().length && !this.submitting && !this.stopReason;
      },

      // ---------------------------------------------------------------- steps
      /** Show a step. ``focusPanel``: move the keyboard focus to it (footer buttons; tabs keep their focus). */
      go(step, focusPanel) {
        this.batch.step = Math.min(4, Math.max(1, step));
        this.$nextTick(() => {
          const top = this.$refs.steps;
          if (top && top.getBoundingClientRect().top < 0) top.scrollIntoView({ block: "start", behavior: "smooth" });
          const panel = focusPanel && this.$root.querySelector('[data-step-panel="' + this.batch.step + '"]');
          if (panel) panel.focus({ preventScroll: true });
        });
      },
      stepKey(e) {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        e.preventDefault();
        this.go(this.batch.step + (e.key === "ArrowRight" ? 1 : -1));
        this.$nextTick(() => {
          const tab = this.$root.querySelector('[data-step-tab="' + this.batch.step + '"]');
          if (tab) tab.focus();
        });
      },
      stepDone(n) {
        if (n === 1) return this.batch.rows.length > 0;
        if (n === 2) return this.selectedKeys().length > 0 && !this.commonErrors().length;
        if (n === 3) return this.batch.rows.length > 0 && this.batch.rows.every((r) => r.date && r.time);
        if (n === 4) {
          const byRow = this.derived().byRow;
          return (
            this.batch.rows.length > 0 &&
            this.batch.rows.every((r) => {
              const status = (byRow[r.id] || {}).status;
              return ((r.text || "").trim() || r.hashtags.length) && (status === "ok" || status === "warning");
            })
          );
        }
        return false;
      },
      stepAlert(n) {
        if (n === 2) return this.selectedKeys().length > 0 && this.commonErrors().length > 0;
        if (n === 3) return this.caps().length > 0;
        if (n === 4) return this.rowsWithContentErrors() > 0;
        return false;
      },
      stepHint(n) {
        const rows = this.batch.rows.length;
        if (n === 1) return rows ? plural(rows, "vídeo", "vídeos") : "Sube o elige vídeos";
        if (n === 2) {
          const k = this.selectedKeys().length;
          return k ? plural(k, "red", "redes") : "Elige dónde publicar";
        }
        if (n === 3) {
          const dated = this.batch.rows.filter((r) => r.date && r.time).length;
          if (!rows) return "Reparte las fechas";
          const s = this.summary();
          if (dated === rows && s && s.first_date) return shortDate(s.first_date) + (s.last_date !== s.first_date ? " – " + shortDate(s.last_date) : "");
          return dated + " de " + rows + " con fecha";
        }
        if (n === 4) {
          const errors = this.rowsWithContentErrors();
          if (errors) return plural(errors, "vídeo con errores", "vídeos con errores");
          const written = this.batch.rows.filter((r) => (r.text || "").trim()).length;
          return rows ? written + " de " + rows + " con texto" : "Un texto por vídeo";
        }
        return "";
      },
      nextLabel() {
        return ["", "Siguiente: redes y ajustes", "Siguiente: calendario", "Siguiente: textos", ""][this.batch.step];
      },

      // ---------------------------------------------------------------- accounts & networks
      selectedKeys() {
        return this.accounts.filter((a) => this.batch.accounts[a.key] && this.batch.accounts[a.key].selected).map((a) => a.key);
      },
      selectedAccounts() {
        return this.selectedKeys().map((k) => this.acc[k]);
      },
      isSel(k) {
        return !!(this.batch.accounts[k] && this.batch.accounts[k].selected);
      },
      conf(k) {
        return this.batch.accounts[k];
      },
      opt(k) {
        return this.batch.accounts[k].options;
      },
      net(k) {
        return (this.acc[k] || {}).network;
      },
      toggleAccount(k) {
        const a = this.acc[k];
        const c = this.batch.accounts[k];
        if (!a || !c) return;
        if (!c.selected && !priv.formatChosen.has(k)) this.setFormat(k, this.defaultFormat(a));
        c.selected = !c.selected;
        if (c.selected) this.onSelected(k);
      },
      chooseFormat(k, key) {
        priv.formatChosen.add(k);
        this.setFormat(k, key);
      },
      onSelected(k) {
        const network = this.net(k);
        if (network === "tiktok") this.loadCreator(k);
        if (network === "youtube") this.loadYtCategories(k);
      },
      formatSpec(a, key) {
        return ((a && a.formats) || []).find((f) => f.key === key) || null;
      },
      enabledFormats(k) {
        return ((this.acc[k] || {}).formats || []).filter((f) => !f.disabled);
      },
      /** Default format for videos: vertical → Reel/Short, horizontal → Facebook post / YouTube video. */
      defaultFormat(a) {
        const items = this.batch && this.batch.rows ? this.batch.rows.map((r) => this.media[r.media_id]).filter(Boolean) : [];
        const vertical = items.filter((m) => orientationOf(m) === "vertical").length;
        const horizontal = items.filter((m) => orientationOf(m) === "horizontal").length;
        const table = horizontal > vertical ? HORIZONTAL_DEFAULTS : VERTICAL_DEFAULTS;
        const want = table[a.network];
        const spec = this.formatSpec(a, want);
        if (spec && !spec.disabled) return want;
        return a.default_format;
      },
      setFormat(k, key) {
        const a = this.acc[k];
        const spec = this.formatSpec(a, key);
        if (!spec || spec.disabled) {
          if (spec && spec.disabled) toast(spec.disabled, "info");
          return;
        }
        const c = this.batch.accounts[k];
        const defaults = clone(spec.defaults || {});
        const kept = {};
        Object.keys(c.options || {}).forEach((opt) => {
          if (opt in defaults || opt === "commercial_content" || opt.startsWith("demo_")) kept[opt] = c.options[opt];
        });
        c.options = Object.assign(defaults, kept);
        c.format = key;
      },
      formatLabel(k) {
        const spec = this.formatSpec(this.acc[k], this.conf(k).format);
        return spec ? spec.label : "";
      },
      accountLabel(k) {
        const a = this.acc[k] || {};
        return a.network_label + (a.display && a.network !== "demo" ? " · " + a.display : "");
      },
      autoHelp(k) {
        const c = this.conf(k);
        if (this.net(k) === "tiktok" && c.options.mode === "inbox") return "El vídeo llegará a la app de TikTok como borrador para que lo termines allí.";
        if (c.auto_publish) return "Se publica sola a la hora indicada (con la aplicación abierta).";
        return "Publicación asistida: a la hora indicada te avisaremos para que la publiques tú.";
      },

      // ---------------------------------------------------------------- TikTok
      modesOf(a) {
        const tt = (a && a.tiktok) || null;
        return tt && Array.isArray(tt.modes) ? tt.modes : ["direct", "inbox"];
      },
      ttModes(k) {
        return this.modesOf(this.acc[k]);
      },
      ttModeLocked(k) {
        return this.ttModes(k).length < 2;
      },
      ttModeNote(k) {
        const tt = (this.acc[k] || {}).tiktok;
        return (tt && tt.note) || "";
      },
      ttDirect(k) {
        return this.opt(k).mode !== "inbox" && this.ttModes(k).includes("direct");
      },
      setInbox(k, on) {
        this.opt(k).mode = on ? "inbox" : "direct";
      },
      async loadCreator(k) {
        const a = this.acc[k];
        if (!a || a.network !== "tiktok" || !a.id || !this.ttModes(k).includes("direct")) return;
        this.creator[k] = Object.assign({}, this.creator[k] || {}, { loading: true, error: null });
        try {
          const res = await App.fetchJSON(this.cfg.urls.creator_info.replace("{id}", encodeURIComponent(a.id)));
          this.creator[k] = { loading: false, info: res.creator_info || null, error: res.ok ? null : res.message || "No se ha podido consultar TikTok." };
        } catch (e) {
          this.creator[k] = { loading: false, info: a.creator_info || null, error: e.message || "No se ha podido consultar TikTok." };
        }
        const info = this.creatorInfo(k);
        const o = this.opt(k);
        const choices = (info && info.privacy_level_options) || [];
        // TikTok's posting UX: no default privacy, even with a single option. A stored value the account no
        // longer offers is cleared so the user chooses again.
        if (choices.length && o.privacy_level && !choices.includes(o.privacy_level)) {
          const label = this.cfg.privacy_labels[o.privacy_level] || o.privacy_level;
          o.privacy_level = null;
          this.notice[k] = "La visibilidad guardada («" + label + "») ya no está disponible en tu cuenta de TikTok: elige otra.";
        }
        if (info) {
          if (info.comment_disabled) o.disable_comment = true;
          if (info.duet_disabled) o.disable_duet = true;
          if (info.stitch_disabled) o.disable_stitch = true;
        }
      },
      creatorInfo(k) {
        const c = this.creator[k];
        return (c && c.info) || (this.acc[k] || {}).creator_info || null;
      },
      creatorName(k) {
        const info = this.creatorInfo(k);
        const a = this.acc[k] || {};
        return (info && (info.nickname || info.username)) || a.name || "tu cuenta";
      },
      privacyChoices(k) {
        const info = this.creatorInfo(k);
        return ((info && info.privacy_level_options) || []).map((value) => ({ value, label: this.cfg.privacy_labels[value] || value }));
      },
      unaudited(k) {
        const opts = ((this.creatorInfo(k) || {}).privacy_level_options || []);
        return opts.length === 1 && opts[0] === "SELF_ONLY";
      },
      creatorFlag(k, flag) {
        const info = this.creatorInfo(k);
        return !!(info && info[flag]);
      },
      allow(k, key) {
        return !this.opt(k)[key];
      },
      setAllow(k, key, on) {
        this.opt(k)[key] = !on;
      },
      setPrivacy(k, value) {
        this.opt(k).privacy_level = value || null;
        this.notice[k] = "";
      },
      setCommercial(k, on) {
        const o = this.opt(k);
        o.commercial_content = !!on;
        if (!on) {
          o.brand_organic_toggle = false;
          o.brand_content_toggle = false;
        }
      },
      setBranded(k, on) {
        const o = this.opt(k);
        o.brand_content_toggle = !!on;
        if (on && o.privacy_level === "SELF_ONLY") {
          o.privacy_level = null;
          this.notice[k] = "El contenido de marca no puede ser privado: elige otra visibilidad.";
        } else this.notice[k] = "";
      },
      commercialLabel(k) {
        const o = this.opt(k);
        if (o.brand_content_toggle) return "Tus vídeos se etiquetarán como «Colaboración pagada».";
        if (o.brand_organic_toggle) return "Tus vídeos se etiquetarán como «Contenido promocional».";
        return "Elige al menos una opción: «Tu marca», «Contenido de marca» o ambas.";
      },
      needsConsent() {
        return this.selectedKeys().some((k) => this.net(k) === "tiktok" && this.ttDirect(k));
      },
      maxDuration(k) {
        const info = this.creatorInfo(k);
        return info && info.max_video_post_duration_sec ? info.max_video_post_duration_sec : null;
      },

      // ---------------------------------------------------------------- YouTube
      async loadYtCategories(k) {
        const a = this.acc[k];
        if (!a || !a.id || (this.ytCats[k] && this.ytCats[k].items)) return;
        this.ytCats[k] = { loading: true, items: null };
        try {
          const res = await App.fetchJSON(this.cfg.urls.yt_categories.replace("{id}", encodeURIComponent(a.id)));
          this.ytCats[k] = { loading: false, items: res.items || [] };
        } catch (e) {
          this.ytCats[k] = { loading: false, items: null };
        }
      },
      ytCategoryChoices(k) {
        const loaded = this.ytCats[k] && this.ytCats[k].items;
        return loaded && loaded.length ? loaded : this.cfg.yt_categories_default || [];
      },
      setKids(k, value) {
        const o = this.opt(k);
        o.made_for_kids = value;
        if (value === true && o.first_comment) o.first_comment = "";
      },
      hasYoutube(row) {
        return this.rowKeys(row).some((k) => this.net(k) === "youtube");
      },
      ytTitleDerived(row) {
        const m = this.media[row.media_id] || {};
        const name = cleanFilename(m.name);
        const title = this.batch.yt_title_source === "filename" ? name || firstLine(row.text) : firstLine(row.text) || name;
        return title.slice(0, (this.cfg.limits && this.cfg.limits.yt_title) || 100);
      },
      ytTitle(row) {
        return row.yt_title || this.ytTitleDerived(row);
      },
      ytTitleLength(row) {
        return this.ytTitle(row).length;
      },

      // ---------------------------------------------------------------- hashtags (chips)
      addTags(target, key, raw) {
        const list = target[key] || [];
        const add = normalizeTags(raw);
        const seen = new Set(list.map((t) => t.toLowerCase()));
        add.forEach((t) => {
          if (!seen.has(t.toLowerCase())) {
            seen.add(t.toLowerCase());
            list.push(t);
          }
        });
        target[key] = list.slice(0, 60);
      },
      removeTag(target, key, index) {
        const list = (target[key] || []).slice();
        list.splice(index, 1);
        target[key] = list;
      },
      tagKey(e, target, key, draftKey) {
        if (e.key === "Enter" || e.key === "," || e.key === " ") {
          if (!(this.drafts[draftKey] || "").trim()) {
            if (e.key === "Enter") e.preventDefault();
            return;
          }
          e.preventDefault();
          this.commitTag(target, key, draftKey);
        } else if (e.key === "Backspace" && !(this.drafts[draftKey] || "")) {
          const list = target[key] || [];
          if (list.length) this.removeTag(target, key, list.length - 1);
        }
      },
      commitTag(target, key, draftKey) {
        const raw = this.drafts[draftKey] || "";
        if (!raw.trim()) return;
        this.addTags(target, key, raw);
        this.drafts[draftKey] = "";
      },
      tagPaste(e, target, key, draftKey) {
        const text = (e.clipboardData && e.clipboardData.getData("text")) || "";
        if (/[\s,;]/.test(text.trim())) {
          e.preventDefault();
          this.addTags(target, key, (this.drafts[draftKey] || "") + " " + text);
          this.drafts[draftKey] = "";
        }
      },
      async applyTagsToAll(row) {
        const others = this.batch.rows.filter((r) => r !== row && r.hashtags.length);
        if (others.length) {
          const ok = await confirmDialog("Los demás vídeos tienen sus propios hashtags. ¿Sustituirlos por los de este vídeo?", {
            title: "Aplicar hashtags a todos",
            ok: "Sí, sustituir",
          });
          if (!ok) return;
        }
        this.batch.rows.forEach((r) => (r.hashtags = row.hashtags.slice()));
        toast("Hashtags aplicados a " + plural(this.batch.rows.length, "vídeo", "vídeos") + ".", "success");
      },

      // ---------------------------------------------------------------- counters
      rowKeys(row) {
        return this.selectedKeys().filter((k) => row.networks[k] !== false);
      },
      caption(row) {
        return composeCaption(row.text, row.hashtags, this.batch.common_hashtags);
      },
      /** Strictest text limit of the row's networks: {length, limit, network, over}. */
      counter(row) {
        const text = this.caption(row);
        let best = null;
        this.rowKeys(row).forEach((k) => {
          const a = this.acc[k];
          const spec = this.formatSpec(a, this.conf(k).format);
          if (!spec || !spec.limit) return;
          const length = textLength(a.unit, text);
          const ratio = length / spec.limit;
          if (!best || ratio > best.ratio || (ratio === best.ratio && spec.limit < best.limit)) {
            best = { length, limit: spec.limit, network: a.network, label: a.network_label, ratio };
          }
        });
        if (!best) return { length: textLength("chars", text), limit: null, network: null, over: false };
        best.over = best.length > best.limit;
        return best;
      },
      counterText(row) {
        const c = this.counter(row);
        return c.limit ? c.length + " / " + c.limit : String(c.length);
      },
      hasInstagram(row) {
        return this.rowKeys(row).some((k) => this.net(k) === "instagram");
      },
      hashtagCount(row) {
        return countHashtags(this.caption(row));
      },

      // ---------------------------------------------------------------- per-row networks
      rowNetOn(row, k) {
        return row.networks[k] !== false;
      },
      toggleRowNet(row, k) {
        const nets = Object.assign({}, row.networks);
        if (nets[k] === false) delete nets[k];
        else nets[k] = false;
        row.networks = nets;
      },

      // ---------------------------------------------------------------- media & uploads
      thumb(row) {
        const m = this.media[row.media_id];
        return m ? m.thumb_url : "";
      },
      mediaName(row) {
        const m = this.media[row.media_id];
        return m ? m.name : "Vídeo no disponible";
      },
      mediaMeta(row) {
        const m = this.media[row.media_id];
        if (!m) return "Ya no está en la biblioteca";
        const parts = [];
        if (m.duration_s) parts.push(duration(m.duration_s));
        if (m.width && m.height) parts.push(m.width + "×" + m.height);
        const o = orientationOf(m);
        if (o) parts.push({ vertical: "Vertical", horizontal: "Horizontal", square: "Cuadrado" }[o]);
        return parts.join(" · ");
      },
      mediaDuration(row) {
        const m = this.media[row.media_id];
        return m && m.duration_s ? duration(m.duration_s) : "";
      },
      compat(row, network) {
        const m = this.media[row.media_id];
        return (m && m.compat && m.compat[network]) || null;
      },
      compatStatus(row, network) {
        const c = this.compat(row, network);
        return c ? c.status : "unknown";
      },
      compatTitle(row, network) {
        const c = this.compat(row, network);
        const label = { facebook: "Facebook", instagram: "Instagram", tiktok: "TikTok", youtube: "YouTube" }[network] || network;
        if (!c) return label + ": sin datos";
        if (c.status === "ok") return label + ": compatible";
        const first = (c.issues || [])[0];
        return label + (c.status === "error" ? ": no compatible" : ": con avisos") + (first ? " — " + first.message : "");
      },
      /** Rows for library items (videos only, no duplicates). ``seq``: upload order — files finish uploading in
       * any order (2 at a time) but keep the order they were chosen in. */
      addMedia(items, seq) {
        const max = (this.cfg.limits && this.cfg.limits.max_rows) || 200;
        let added = 0;
        let dup = 0;
        let notVideo = 0;
        for (const item of items) {
          if (!item || item.id == null) continue;
          if (item.kind && item.kind !== "video") {
            notVideo++;
            continue;
          }
          this.media[item.id] = Object.assign({}, this.media[item.id] || {}, item);
          if (this.batch.rows.some((r) => r.media_id === item.id)) {
            dup++;
            continue;
          }
          if (this.batch.rows.length >= max) {
            toast("Una carga masiva admite como máximo " + max + " vídeos.", "warning");
            break;
          }
          const row = { id: uid(), media_id: item.id, text: "", hashtags: [], yt_title: "", date: "", time: "", networks: {} };
          let at = this.batch.rows.length;
          if (seq != null) {
            priv.rowSeq[row.id] = seq;
            const later = this.batch.rows.findIndex((r) => priv.rowSeq[r.id] != null && priv.rowSeq[r.id] > seq);
            if (later >= 0) at = later;
          }
          this.batch.rows.splice(at, 0, row);
          added++;
        }
        if (dup) toast(dup === 1 ? "Ese vídeo ya estaba en la lista." : dup + " vídeos ya estaban en la lista.", "info");
        if (notVideo) toast("La carga masiva es solo para vídeos: " + plural(notVideo, "archivo omitido", "archivos omitidos") + ".", "warning");
        const missing = items.filter((i) => i && i.id != null && !(this.media[i.id] && this.media[i.id].compat)).map((i) => i.id);
        if (missing.length) this.loadMediaInfo(missing);
        return added;
      },
      async loadMediaInfo(ids) {
        try {
          const res = await App.fetchJSON(this.cfg.urls.media + "?ids=" + ids.join(","));
          Object.keys(res.items || {}).forEach((id) => (this.media[id] = Object.assign({}, this.media[id] || {}, res.items[id])));
        } catch (e) {
          /* compatibility badges stay "sin datos" */
        }
      },
      async removeRow(index) {
        const row = this.batch.rows[index];
        if (!row) return;
        if ((row.text || "").trim() || row.hashtags.length) {
          const ok = await confirmDialog("Se quitará «" + this.mediaName(row) + "» de la carga masiva (seguirá en la Biblioteca).", {
            title: "Quitar vídeo",
            ok: "Quitar",
            danger: true,
          });
          if (!ok) return;
        }
        this.batch.rows.splice(index, 1);
      },
      /** Move a video in the list. The list order is the publishing order, so the dates stay where they are:
       * each video takes the date and time of its new position. */
      reorder(from, to) {
        const rows = this.batch.rows;
        if (from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) return null;
        const slots = rows.map((r) => [r.date, r.time]);
        const [row] = rows.splice(from, 1);
        rows.splice(to, 0, row);
        const lo = Math.min(from, to);
        const hi = Math.max(from, to);
        for (let i = lo; i <= hi; i++) {
          if (rows[i].date !== slots[i][0]) rows[i].date = slots[i][0];
          if (rows[i].time !== slots[i][1]) rows[i].time = slots[i][1];
        }
        return row;
      },
      moveRow(index, delta) {
        const row = this.reorder(index, index + delta);
        if (!row) return;
        // Keep the keyboard focus on the moved card (x-for re-inserts its node, which drops the focus).
        this.$nextTick(() => requestAnimationFrame(() => {
          const btn = this.$root.querySelector('[data-row="' + row.id + '"] [data-move="' + (delta < 0 ? "up" : "down") + '"]');
          if (btn && !btn.disabled) btn.focus();
          else {
            const other = this.$root.querySelector('[data-row="' + row.id + '"] [data-move]:not([disabled])');
            if (other) other.focus();
          }
        }));
      },
      onDragStart(index, e) {
        this.dragIndex = index;
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", String(index));
        }
      },
      onDragOver(index, e) {
        if (this.dragIndex == null) return;
        e.preventDefault();
        this.dragOver = index;
      },
      onDropRow(index, e) {
        if (this.dragIndex == null) return;
        e.preventDefault();
        const from = this.dragIndex;
        this.dragIndex = null;
        this.dragOver = null;
        this.reorder(from, index);
      },
      hasDates() {
        return this.batch.rows.some((r) => r.date || r.time);
      },
      onDragEnd() {
        this.dragIndex = null;
        this.dragOver = null;
      },

      pickFiles() {
        if (this.$refs.file) this.$refs.file.click();
      },
      onFileInput(e) {
        const files = e.target.files;
        if (files && files.length) this.queueFiles(files);
        e.target.value = "";
      },
      onDragEnterZone(e) {
        const types = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
        if (types.includes("Files")) {
          e.preventDefault();
          this.dropping = true;
        }
      },
      onDropZone(e) {
        this.dropping = false;
        if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
        e.preventDefault();
        this.queueFiles(e.dataTransfer.files);
      },
      queueFiles(fileList) {
        const files = Array.from(fileList);
        const videos = files.filter((f) => (f.type || "").startsWith("video/") || VIDEO_EXT.test(f.name));
        const skipped = files.length - videos.length;
        if (skipped) toast("La carga masiva es solo para vídeos: " + plural(skipped, "archivo omitido", "archivos omitidos") + ".", "warning");
        videos.forEach((file) => {
          const entry = { id: uid(), name: file.name, size: file.size, progress: 0, status: "queued", error: "" };
          this.uploads.push(entry);
          priv.queue.push({ entry: entry.id, file, seq: ++priv.seq });
        });
        this.pump();
      },
      uploadEntry(id) {
        return this.uploads.find((u) => u.id === id);
      },
      pump() {
        while (priv.active < UPLOAD_PARALLEL && priv.queue.length) {
          const job = priv.queue.shift();
          priv.active++;
          this.upload(job).finally(() => {
            priv.active--;
            this.pump();
          });
        }
      },
      upload(job) {
        return new Promise((resolve) => {
          const u = this.uploadEntry(job.entry);
          if (!u) return resolve();
          u.status = "uploading";
          const fd = new FormData();
          fd.append("csrf_token", App.csrfToken ? App.csrfToken() : "");
          fd.append("files", job.file, job.file.name);
          const xhr = new XMLHttpRequest();
          xhr.open("POST", this.cfg.urls.upload);
          xhr.setRequestHeader("Accept", "application/json");
          if (App.csrfToken) xhr.setRequestHeader("X-CSRF-Token", App.csrfToken());
          xhr.upload.onprogress = (ev) => {
            const e = this.uploadEntry(job.entry);
            if (e && ev.lengthComputable) e.progress = Math.round((ev.loaded * 100) / ev.total);
          };
          xhr.onload = () => {
            const e = this.uploadEntry(job.entry);
            let data = null;
            try {
              data = JSON.parse(xhr.responseText);
            } catch (err) {
              data = null;
            }
            const ok = Array.isArray(data) ? data.filter((r) => r.ok) : [];
            if (ok.length) {
              this.addMedia(ok, job.seq);
              if (e) {
                e.status = "done";
                e.progress = 100;
              }
              setTimeout(() => (this.uploads = this.uploads.filter((x) => x.id !== job.entry)), 1600);
            } else if (e) {
              e.status = "error";
              const failed = Array.isArray(data) ? data.find((r) => !r.ok) : null;
              e.error = (failed && failed.error) || (App.errorMessage ? App.errorMessage(xhr.status, xhr.getResponseHeader("X-Error-Message"), xhr.getResponseHeader("Content-Type"), xhr.responseText) : "No se ha podido subir.");
            }
            resolve();
          };
          xhr.onerror = () => {
            const e = this.uploadEntry(job.entry);
            if (e) {
              e.status = "error";
              e.error = "No se ha podido subir. ¿Sigue abierta la aplicación?";
            }
            resolve();
          };
          xhr.send(fd);
        });
      },
      dismissUpload(id) {
        this.uploads = this.uploads.filter((u) => u.id !== id);
      },
      uploadsBusy() {
        return this.uploads.some((u) => u.status === "queued" || u.status === "uploading");
      },
      openPicker() {
        const host = doc.getElementById("modal-root");
        if (!window.htmx || !host) return toast("No se ha podido abrir la biblioteca.", "error");
        this.pickerPending = true;
        window.htmx.ajax("GET", this.cfg.urls.picker, { target: host, swap: "innerHTML" });
      },
      onMediaSelected(e) {
        if (!this.pickerPending) return;
        this.pickerPending = false;
        const items = (e.detail && e.detail.items) || [];
        const added = this.addMedia(items);
        if (added) toast(plural(added, "vídeo añadido", "vídeos añadidos") + ".", "success");
      },

      // ---------------------------------------------------------------- calendar
      setMode(mode) {
        this.batch.distribution.mode = mode;
      },
      addSlot() {
        const v = (this.newSlot || "").trim();
        if (!/^\d{1,2}:\d{2}$/.test(v)) {
          toast("Elige una hora (por ejemplo 12:00).", "warning");
          return;
        }
        const hm = pad(Number(v.split(":")[0])) + ":" + v.split(":")[1];
        const slots = this.batch.distribution.slots.slice();
        if (slots.includes(hm)) return toast("Esa hora ya está en la lista.", "info");
        if (slots.length >= ((this.cfg.limits && this.cfg.limits.max_slots) || 12)) return toast("Has alcanzado el máximo de horas.", "warning");
        slots.push(hm);
        slots.sort();
        this.batch.distribution.slots = slots;
        this.newSlot = "";
      },
      removeSlot(i) {
        const slots = this.batch.distribution.slots.slice();
        slots.splice(i, 1);
        this.batch.distribution.slots = slots;
      },
      useSuggested() {
        this.batch.distribution.slots = (this.cfg.suggested_slots || []).slice();
      },
      slotsHint() {
        const src = this.cfg.slots_source;
        if (src === "datos") return "Sugeridas según el rendimiento de tus publicaciones.";
        if (src === "patron_general") return "Sugeridas según el patrón general de horas con más actividad.";
        return "";
      },
      perDayError() {
        const d = this.batch.distribution;
        if (d.mode !== "per_day") return "";
        if (!d.slots.length) return "Añade al menos una hora.";
        if (Number(d.per_day) > d.slots.length) return "Para " + d.per_day + " publicaciones al día añade al menos " + d.per_day + " horas distintas.";
        return "";
      },
      planPreview() {
        const d = this.batch.distribution;
        const n = this.batch.rows.length;
        if (!n) return "Añade vídeos para repartirlos.";
        if (d.mode === "per_day") {
          const per = Math.max(1, Number(d.per_day) || 1);
          const days = Math.ceil(n / per);
          return plural(n, "vídeo", "vídeos") + " a " + per + " al día ≈ " + plural(days, "día", "días") + (d.skip_weekends ? " laborables" : "") + ".";
        }
        const every = Math.max(1, Number(d.every_days) || 1);
        return plural(n, "vídeo", "vídeos") + ", uno cada " + plural(every, "día", "días") + (d.skip_weekends ? " (sin fines de semana)" : "") + ".";
      },
      async distribute() {
        if (!this.batch.rows.length || this.distributing) return;
        if (this.perDayError()) return toast(this.perDayError(), "warning");
        const dated = this.batch.rows.filter((r) => r.date || r.time).length;
        if (dated) {
          const ok = await confirmDialog("Se cambiarán las fechas de " + plural(dated, "vídeo que ya tiene fecha", "vídeos que ya tienen fecha") + ".", {
            title: "Repartir fechas",
            ok: "Repartir",
          });
          if (!ok) return;
        }
        this.distributing = true;
        this.distError = "";
        try {
          const res = await App.fetchJSON(this.cfg.urls.distribute, {
            body: { distribution: clone(this.batch.distribution), count: this.batch.rows.length, brand_id: this.batch.brand_id },
          });
          (res.items || []).forEach((it, i) => {
            const row = this.batch.rows[i];
            if (row) {
              row.date = it.date;
              row.time = it.time;
            }
          });
          if (res.start_date) this.batch.distribution.start_date = res.start_date;
          this.distNotes = res.notes || [];
          if (this.report) this.report = Object.assign({}, this.report, { summary: res.summary });
          else this.report = { summary: res.summary, caps: [], common: { errors: [], warnings: [] }, rows: {} };
          toast("Fechas repartidas: " + (res.summary && res.summary.first_label ? "del " + res.summary.first_label + " al " + res.summary.last_label : "listo") + ".", "success");
        } catch (e) {
          if (this.conflict(e)) return;
          this.distError = e.message || "No se han podido repartir las fechas.";
        } finally {
          this.distributing = false;
        }
      },
      clearDates() {
        this.batch.rows.forEach((r) => {
          r.date = "";
          r.time = "";
        });
      },
      shortDate,
      dayBars() {
        const s = this.summary();
        return (s && s.per_day) || [];
      },
      maxPerDay() {
        const s = this.summary();
        return (s && s.max_per_day) || 1;
      },
      undatedCount() {
        return this.batch.rows.filter((r) => !(r.date && r.time)).length;
      },
      autoCount() {
        return (this.report && this.report.auto_rows) || 0;
      },

      // ---------------------------------------------------------------- texts helpers
      openPaste() {
        this.paste.open = true;
        this.paste.text = "";
        doc.documentElement.classList.add("has-layer");
        this.$nextTick(() => this.$refs.pasteText && this.$refs.pasteText.focus());
      },
      closePaste() {
        this.paste.open = false;
        doc.documentElement.classList.remove("has-layer");
        this.$nextTick(() => this.$refs.pasteBtn && this.$refs.pasteBtn.focus());
      },
      pasteParts() {
        return splitPasted(this.paste.text, this.paste.mode);
      },
      /** Preview of one pasted text: its lines, without the blank ones (3 lines shown). */
      pastePreview(part) {
        return String(part || "").replace(/\n(?:[ \t]*\n)+/g, "\n");
      },
      pasteDetected() {
        if (this.paste.mode !== "auto" || !(this.paste.text || "").trim()) return "";
        return { sep: "separados por líneas ---", blank: "separados por líneas en blanco", line: "uno por línea" }[pasteMethod(this.paste.text, "auto")];
      },
      pasteTargets() {
        return this.paste.onlyEmpty ? this.batch.rows.filter((r) => !(r.text || "").trim()) : this.batch.rows;
      },
      pasteSummary() {
        const parts = this.pasteParts().length;
        const rows = this.pasteTargets().length;
        if (!parts) return "Pega los textos para ver cuántos se detectan.";
        const how = this.pasteDetected();
        let msg = "Se han detectado " + plural(parts, "texto", "textos") + (how ? " (" + how + ")" : "") + " para " + plural(rows, "vídeo", "vídeos") + ".";
        if (parts > rows) msg += " Sobran " + (parts - rows) + ": no se usarán.";
        else if (parts < rows) msg += " " + plural(rows - parts, "vídeo se queda", "vídeos se quedan") + " sin texto nuevo.";
        return msg;
      },
      applyPaste() {
        const parts = this.pasteParts();
        if (!parts.length) return;
        const targets = this.pasteTargets();
        let n = 0;
        targets.forEach((row, i) => {
          if (i < parts.length) {
            row.text = parts[i];
            n++;
          }
        });
        this.closePaste();
        toast(plural(n, "texto pegado", "textos pegados") + " en orden.", "success");
      },
      pasteKey(e) {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          this.closePaste();
        } else if (e.key === "Tab") {
          const box = this.$refs.pasteDialog;
          const items = Array.from(box.querySelectorAll("button:not([disabled]), textarea, input:not([disabled]), select")).filter((el) => el.offsetParent !== null);
          if (!items.length) return;
          const first = items[0];
          const last = items[items.length - 1];
          if (e.shiftKey && doc.activeElement === first) {
            e.preventDefault();
            last.focus();
          } else if (!e.shiftKey && doc.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          this.applyPaste();
        }
      },
      async useFileNames() {
        const empty = this.batch.rows.filter((r) => !(r.text || "").trim());
        let rows = empty;
        if (!empty.length) {
          const ok = await confirmDialog("Todos los vídeos tienen texto. ¿Sustituirlos por el nombre de cada archivo?", { title: "Usar el nombre del archivo", ok: "Sustituir" });
          if (!ok) return;
          rows = this.batch.rows;
        }
        rows.forEach((r) => (r.text = cleanFilename((this.media[r.media_id] || {}).name)));
        toast(plural(rows.length, "texto rellenado", "textos rellenados") + " con el nombre del archivo.", "success");
      },
      visibleRows() {
        if (!this.onlyIssues) return this.batch.rows;
        return this.batch.rows.filter((r) => this.rowStatus(r) === "error" || this.rowStatus(r) === "warning");
      },
      rowLabel(i) {
        return "Vídeo " + (i + 1);
      },
      rowWhen(row) {
        if (!(row.date && row.time)) return "Sin fecha";
        return shortDate(row.date) + ", " + row.time;
      },
      rowPast(row) {
        if (!(row.date && row.time)) return false;
        return row.date + "T" + row.time <= (this.cfg.now_local || "");
      },
      issueList(row) {
        const info = this.rowInfo(row);
        return info.errors.concat(info.own);
      },
      /** Grow a caption box with its text. Browsers with ``field-sizing: content`` do it in CSS; elsewhere the
       * boxes waiting for it are measured together once per frame (one layout instead of one per video). */
      autosize(el) {
        if (!el || FIELD_SIZING) return;
        priv.sizeQueue.add(el);
        if (priv.sizeFrame) return;
        priv.sizeFrame = requestAnimationFrame(() => {
          const els = Array.from(priv.sizeQueue).filter((x) => x.isConnected);
          priv.sizeQueue.clear();
          priv.sizeFrame = 0;
          els.forEach((x) => (x.style.height = "auto"));
          const heights = els.map((x) => x.scrollHeight);
          els.forEach((x, i) => (x.style.height = Math.min(Math.max(heights[i] + 2, 84), 360) + "px"));
        });
      },

      // ---------------------------------------------------------------- submit / discard
      scheduleLabel() {
        const n = this.batch.rows.length;
        return "Programar todo" + (n ? " (" + n + ")" : "");
      },
      async submit(action) {
        if (this.submitting || priv.stopped) return;
        if (!this.batch.rows.length && !this.interrupted) return toast("Añade al menos un vídeo.", "warning");
        if (this.uploadsBusy()) return toast("Espera a que terminen de subirse los vídeos.", "warning");
        if (action === "schedule") {
          if (!this.selectedKeys().length) {
            this.go(2);
            return toast("Elige al menos una red en el paso 2.", "warning");
          }
          if (this.commonErrors().length) {
            this.go(2);
            return toast(this.commonErrors()[0].message, "warning");
          }
          const bad = this.rowsWithErrors();
          let msg = "Se van a programar " + plural(this.batch.rows.length - bad, "publicación", "publicaciones") + ".";
          if (!this.batch.rows.length) {
            msg = "Se terminará la carga masiva que se interrumpió: los vídeos ya guardados no se repiten.";
          } else if (bad && bad === this.batch.rows.length) {
            msg = "Todos los vídeos tienen algún error: se guardarán como borradores para que los revises.";
          } else if (bad) {
            msg += " " + plural(bad, "vídeo tiene", "vídeos tienen") + " errores y se " + (bad === 1 ? "guardará" : "guardarán") + " como borrador para que lo" + (bad === 1 ? "" : "s") + " revises.";
          }
          if (this.autoCount()) msg += "\n\nRecuerda: el ordenador tiene que estar encendido y la aplicación abierta a esas horas.";
          const ok = await confirmDialog(msg, { title: "Programar todo", ok: "Programar" });
          if (!ok) return;
        } else {
          const ok = await confirmDialog("Se guardará cada vídeo como una publicación en borrador (no se publicará nada).", { title: "Guardar como borradores", ok: "Guardar borradores" });
          if (!ok) return;
        }
        this.submitting = true;
        clearTimeout(priv.saveTimer);
        try {
          await this.save();
          const res = await App.fetchJSON(this.cfg.urls.submit, { body: { batch: clone(this.batch), action, tiktok_consent: this.consent } });
          this.finishSubmit(res);
        } catch (e) {
          if (this.conflict(e)) return;
          if (e.data && e.data.report) {
            this.report = e.data.report;
            this.checks = Object.assign({}, this.checks, e.data.report.rows || {});
            if (e.data.code === "common_errors") this.go(2);
          }
          toast(e.message || "No se ha podido guardar la carga masiva.", "error");
        } finally {
          this.submitting = false;
        }
      },
      finishSubmit(res) {
        this.result = res;
        this.consent = false;
        const next = res.remaining_batch ? this.normalizeBatch(res.remaining_batch) : this.normalizeBatch(this.freshBatch(res));
        this.checks = {};
        this.report = null;
        this.batch = next;
        // A new batch (new token): autosave and validation start again from what the server has.
        this.resume();
        clearTimeout(priv.saveTimer);
        priv.lastSaved = JSON.stringify(this.batch);
        this.snapshotRows();
        this.saveState = priv.stopped ? "stopped" : res.remaining_batch ? "saved" : "idle";
        this.savedAt = null;
        this.restored = false;
        this.interrupted = null;
        window.scrollTo({ top: 0, behavior: "smooth" });
        this.$nextTick(() => this.$refs.result && this.$refs.result.focus());
        if (res.remaining_batch) this.scheduleCheck(true);
      },
      /** A new empty batch keeping the common settings (networks, options, hashtags, times). */
      freshBatch(res) {
        const b = clone(this.batch);
        b.token = (res && res.new_token) || this.newToken();
        b.rows = [];
        b.step = 1;
        b.distribution.start_date = this.cfg.today || "";
        this.resetBatchChoices(b.accounts);
        return b;
      },
      /** For a new batch the choices TikTok and YouTube require the user to make each time start empty again:
       * TikTok's visibility (no default, rules._DEFAULT_OPTIONS) and YouTube's "made for kids". */
      resetBatchChoices(accounts) {
        Object.keys(accounts || {}).forEach((k) => {
          const c = accounts[k];
          if (!c || !c.options) return;
          if (this.net(k) === "tiktok") c.options.privacy_level = null;
          if (this.net(k) === "youtube") c.options.made_for_kids = null;
          this.notice[k] = "";
        });
        return accounts;
      },
      newToken() {
        const bytes = new Uint8Array(18);
        (window.crypto || window.msCrypto).getRandomValues(bytes);
        return btoa(String.fromCharCode.apply(null, bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
      },
      newBatchFromResult() {
        this.result = null;
        this.go(1);
      },
      resultTitle() {
        const r = this.result;
        if (!r) return "";
        if (r.action === "draft") return plural(r.drafts, "borrador guardado", "borradores guardados");
        if (!r.scheduled) return "No se ha programado ninguna publicación";
        return plural(r.scheduled, "publicación programada", "publicaciones programadas");
      },
      resultText() {
        const r = this.result;
        if (!r) return "";
        const parts = [];
        if (r.action === "schedule" && r.drafts) parts.push(plural(r.drafts, "vídeo se ha guardado", "vídeos se han guardado") + " como borrador porque tenía" + (r.drafts === 1 ? "" : "n") + " algún error: revísalo" + (r.drafts === 1 ? "" : "s") + " más abajo.");
        if (r.failed) parts.push(plural(r.failed, "vídeo no se ha podido guardar", "vídeos no se han podido guardar") + " y sigue" + (r.failed === 1 ? "" : "n") + " en la carga masiva.");
        if (r.first_label && r.last_label) parts.push(r.first_label === r.last_label ? "Fecha: " + r.first_label + "." : "Del " + r.first_label + " al " + r.last_label + ".");
        if (r.duplicate) parts.push("Esta carga ya se había guardado: no se ha creado nada nuevo.");
        return parts.join(" ");
      },
      postUrl(id) {
        return this.cfg.urls.post.replace("{id}", id);
      },
      async discard() {
        const n = this.batch.rows.length;
        const ok = await confirmDialog(
          n ? "Se quitarán los " + plural(n, "vídeo", "vídeos") + " y sus textos de la carga masiva. Los vídeos seguirán en la Biblioteca y no se borra nada programado." : "Se reiniciará la carga masiva.",
          { title: "Descartar lote", ok: "Descartar", danger: true }
        );
        if (!ok) return;
        clearTimeout(priv.saveTimer);
        try {
          const res = await App.fetchJSON(this.cfg.urls.discard, { method: "POST", body: { brand_id: this.batch.brand_id } });
          const keepAccounts = this.resetBatchChoices(clone(this.batch.accounts));
          this.batch = this.normalizeBatch(Object.assign({}, res.batch, { accounts: keepAccounts }));
          this.checks = {};
          this.report = null;
          this.result = null;
          this.uploads = [];
          priv.queue = [];
          this.resume();
          priv.lastSaved = "";
          this.snapshotRows();
          this.saveState = priv.stopped ? "stopped" : "idle";
          this.savedAt = null;
          this.restored = false;
          this.interrupted = null;
          this.consent = false;
          toast("Carga masiva descartada.", "success");
        } catch (e) {
          if (this.conflict(e)) return;
          toast(e.message || "No se ha podido descartar.", "error");
        }
      },

      // ---------------------------------------------------------------- keyboard
      onKeydown(e) {
        if (this.paste.open) this.pasteKey(e);
      },
    };
  }

  function register(Alpine) {
    if (register.done) return;
    register.done = true;
    Alpine.data("bulkPage", component);
  }
  // Exposed for unit-free sanity checks in the browser console / e2e.
  App.bulk = { composeCaption, normalizeTags, splitPasted, pasteMethod, cleanFilename, textLength };
  if (window.Alpine && window.Alpine.data) register(window.Alpine);
  doc.addEventListener("alpine:init", () => register(window.Alpine));
})();
