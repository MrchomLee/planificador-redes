/* Planificador — composer ("Crear nueva publicación", SPEC §1/§7/§15, layout doc 05 §3 + §5b).
 *
 * Loaded on every page by base.html (the composer opens from anywhere: top bar button, shortcut N, planner
 * slots). Registers the Alpine component `composer`, mounted by templates/composer/_composer.html either as a
 * full-screen overlay inside #modal-root (htmx fragment) or as a full page.
 *
 * State comes from <script type="application/json" data-composer-state> (built by routes/composer.py).
 * Everything is edited client-side; the server validates the same JSON payload on every change (debounced
 * POST /api/composer/validar) and on save (POST /publicaciones[/id]). On success the server answers with an
 * HX-Trigger header (toast, closeModal, planner:refresh, posts:changed) that is replayed here as DOM events.
 *
 * Model:
 *   selected       account keys in display order ("12", or "demo" for the not-yet-created Demo account)
 *   targets[key]   {format, text_override, media_override, options, auto_publish} — kept for every account,
 *                  so unselecting and selecting again restores the options
 *   text / media   the "Común" text and media; per-network tabs edit text_override / media_override
 */
(function () {
  "use strict";

  const doc = document;
  const App = window.App || (window.App = {});

  const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
  const MONTHS_SHORT = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"];
  const DAY_MS = 86400000;
  // The issue panels are pinned under the scrolling editor. Below this window height an expanded error list
  // squeezes the text box and pushes the media toolbar out of view, so it starts collapsed there (its header
  // still shows the count and the first error; fields keep their inline errors).
  const ISSUES_OPEN_MIN_HEIGHT = 860;

  const EMOJI = [
    ["Caras", "😀 😃 😄 😁 😆 😅 😂 🤣 😊 😇 🙂 😉 😍 🥰 😘 😋 😎 🤩 🥳 😏 🤔 🤗 🤭 😴 😮 😱 😢 😭 😤 😡 🥺 🙄 😬 🤯 🥶 🤠"],
    ["Gestos", "👍 👎 👏 🙌 👐 🤝 🙏 💪 👋 🤙 👌 ✌️ 🤞 🫶 👉 👈 👆 👇 ☝️ ✋ 🤘 ✍️ 👀 🧠"],
    ["Corazones", "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💕 💞 💓 💗 💖 💘 💝 ❣️ 💔"],
    ["Celebración", "🎉 🎊 🎈 🎁 🎂 🥂 🍾 🏆 🥇 🎯 ⭐ 🌟 ✨ 💫 🔥 💥 ⚡ 🌈"],
    ["Naturaleza", "☀️ 🌤️ 🌧️ ❄️ 🌊 🌸 🌼 🌻 🌹 🌷 🍀 🌿 🌵 🌴 🍁 🐶 🐱 🦋 🐝"],
    ["Comida", "☕ 🍵 🍷 🍺 🍹 🍕 🍔 🌮 🥗 🍣 🍝 🥐 🍰 🍩 🍪 🍫 🍓 🍉 🥑"],
    ["Objetos", "📣 📢 📌 📍 📅 🗓️ ⏰ 📷 🎥 🎬 🎤 🎧 💻 📱 💡 📚 ✏️ 🛒 💼 🔗 🏠 ✈️ 🚀"],
    ["Símbolos", "✅ ☑️ ❌ ⚠️ ❗ ❓ 💯 🆕 🔝 🔜 ➡️ ⬅️ ⬆️ ⬇️ 🔴 🟢 🔵 ⚪ ⚫ ▶️ ⏩ 💬 💭 #️⃣"],
  ].map(([name, list]) => ({ name, items: list.split(" ") }));

  const FORMAT_LABELS_UP = { post: "POST", reel: "REEL", story: "HISTORIA", video: "VÍDEO", short: "SHORT", photo: "FOTOS" };
  const FIRST_COMMENT_FORMATS = { instagram: ["post", "reel"], youtube: ["video", "short"] };
  const PHONE_FORMATS = { facebook: ["reel", "story"], instagram: ["reel", "story"], tiktok: ["video", "photo"], youtube: ["short"] };

  const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  const pad = (n) => String(n).padStart(2, "0");
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const toast = (msg, kind) => (App.toast ? App.toast(msg, kind) : window.alert(msg));
  const storage = {
    get: (k, d) => (App.storage ? App.storage.get(k, d) : d),
    set: (k, v) => App.storage && App.storage.set(k, v),
  };

  /** Text length in the unit each network counts (Python len = code points; TikTok UTF-16; YouTube bytes). */
  function textLength(unit, text) {
    const t = text || "";
    if (unit === "utf16") return t.length;
    if (unit === "bytes") return new TextEncoder().encode(t).length;
    let n = 0;
    for (const _ of t) n++; // eslint-disable-line no-unused-vars
    return n;
  }
  const HASHTAG_RE = /(^|[^\p{L}\p{M}\p{N}_&])#([\p{L}\p{M}\p{N}_]+)/gu;
  function countHashtags(text) {
    return ((text || "").match(HASHTAG_RE) || []).length;
  }
  function firstLine(text) {
    for (const line of (text || "").split("\n")) {
      const clean = line.replace(/[<>]/g, "").trim();
      if (clean) return clean.slice(0, 100);
    }
    return "";
  }
  function duration(seconds) {
    if (seconds == null || isNaN(seconds)) return "";
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h ? h + ":" + pad(m) + ":" + pad(sec) : m + ":" + pad(sec);
  }
  function humanSeconds(seconds) {
    const s = Math.round(seconds || 0);
    if (s < 60) return s + " s";
    const m = Math.floor(s / 60);
    const r = s % 60;
    return r ? m + " min " + r + " s" : m + " min";
  }
  /** "YYYY-MM-DDTHH:MM" wall time → UTC-floating Date (its UTC fields are the brand wall time). */
  function parseWall(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(s || "");
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0))) : null;
  }
  const wallDate = (d) => d.toISOString().slice(0, 10);
  const wallTime = (d) => d.toISOString().slice(11, 16);

  /** Escaped text with hashtags, mentions and links highlighted (for previews). */
  function richText(text, net) {
    let html = esc(text || "");
    html = html.replace(/(https?:\/\/[^\s<]+)/g, '<span class="pv-link">$1</span>');
    html = html.replace(/(^|[^\p{L}\p{M}\p{N}_&;])#([\p{L}\p{M}\p{N}_]+)/gu, '$1<span class="pv-tag">#$2</span>');
    if (net !== "youtube") html = html.replace(/(^|[^\w@.])@([A-Za-z0-9._]{1,30})/g, '$1<span class="pv-tag">@$2</span>');
    return html.replace(/\n/g, "<br>");
  }

  function component() {
    // Non-reactive per-instance bookkeeping (kept out of Alpine's proxy so effects never re-trigger on it).
    const priv = { baseline: "", lastSnap: "", timer: null, seq: 0, caret: null, unload: null };
    return {
      // ---------------------------------------------------------------- state
      ready: false,
      cfg: { urls: {}, limits: {}, legal: {}, brand: {}, privacy_labels: {}, recent_hashtags: [] },
      ro: false,
      accounts: [],
      acc: {},
      selected: [],
      targets: {},
      text: "",
      media: [],
      mediaIndex: {},
      notes: "",
      showNotes: false,
      ideaStage: null,
      date: "",
      time: "",
      perNetwork: false,
      tab: "common",
      secOpen: { global: false },
      issuesOpen: { error: true, warning: false },
      pvKey: null,
      pvDevice: "mobile",
      pvVisible: true,
      pvExpanded: false,
      result: null,
      validating: false,
      validateFailed: false,
      saving: false,
      savedOK: false,
      dirty: false,
      pop: null,
      emojiCat: 0,
      firstCommentOpen: false,
      linkUrl: "",
      uploads: [],
      dropping: false,
      dragFrom: null,
      creator: {},
      ytCats: {},
      ytLists: {},
      ytTitleTouched: {},
      drafts: {},
      notice: {},
      pickerPurpose: null,
      accountMap: {},
      loadedAt: Date.now(),

      // ---------------------------------------------------------------- lifecycle
      init() {
        this.issuesOpen.error = (window.innerHeight || 0) >= ISSUES_OPEN_MIN_HEIGHT;
        const holder = this.$el.querySelector("script[data-composer-state]");
        let s = {};
        try {
          s = JSON.parse((holder && holder.textContent) || "{}");
        } catch (e) {
          toast("No se ha podido abrir el editor. Recarga la página.", "error");
        }
        this.cfg = Object.assign(this.cfg, s);
        this.ro = !!s.readonly;
        this.accounts = s.accounts || [];
        const acc = {};
        this.accounts.forEach((a) => (acc[a.key] = a));
        this.acc = acc;
        const targets = {};
        this.accounts.forEach((a) => (targets[a.key] = this.newTarget(a, (s.targets || {})[a.key])));
        this.targets = targets;
        this.text = s.text || "";
        this.media = (s.media || []).slice();
        this.mediaIndex = Object.assign({}, s.media_index || {});
        this.media.forEach((m) => (this.mediaIndex[m.id] = m));
        this.notes = s.notes || "";
        this.showNotes = !!this.notes;
        this.ideaStage = s.idea_stage || null;
        if (s.scheduled_local) {
          this.date = s.scheduled_local.slice(0, 10);
          this.time = s.scheduled_local.slice(11, 16);
        }
        let selected = (s.selected || []).filter((k) => acc[k]);
        if (s.mode === "create" && !selected.length) {
          const remembered = storage.get("composer.networks." + (s.brand && s.brand.id), []);
          selected = (Array.isArray(remembered) ? remembered : []).filter((k) => acc[k] && acc[k].status !== "disconnected");
          if (!selected.length && this.accounts.length === 1) selected = [this.accounts[0].key];
        }
        this.selected = this.ordered(selected);
        this.perNetwork = this.selected.length >= 2 && this.selected.some((k) => this.hasOverride(k));
        this.pvKey = this.selected[0] || null;
        this.selected.forEach((k) => {
          this.secOpen[k] = true;
          this.onSelected(k);
        });
        if (this.selected.some((k) => this.firstCommentFor(k))) this.firstCommentOpen = !!this.commonFirstComment;

        this.$el.addEventListener("layer:beforeclose", (e) => this.onBeforeClose(e));
        if (this.cfg.layer === "page") {
          priv.unload = (e) => {
            if (this.dirty && !this.savedOK && !this.ro) {
              e.preventDefault();
              e.returnValue = "";
            }
          };
          window.addEventListener("beforeunload", priv.unload);
        }
        this.$nextTick(() => {
          priv.baseline = this.snapshot();
          priv.lastSnap = priv.baseline;
          this.ready = true;
          this.autosize();
          if (!this.ro) {
            this.validate();
            const ta = this.$refs.text;
            if (ta && this.cfg.mode === "create") ta.focus({ preventScroll: true });
          }
        });
      },

      destroy() {
        if (priv.unload) window.removeEventListener("beforeunload", priv.unload);
        clearTimeout(priv.timer);
      },

      newTarget(a, t) {
        const fmt = (t && t.format) || a.default_format;
        const spec = this.formatSpec(a, fmt) || {};
        const options = Object.assign(clone(spec.defaults || {}), t ? clone(t.options || {}) : {});
        if (a.network === "tiktok") {
          if (!("consent" in options)) options.consent = false;
          if (!("commercial_content" in options)) options.commercial_content = !!(options.brand_organic_toggle || options.brand_content_toggle);
          if (!options.mode) options.mode = "direct";
          // Only the modes the account's TikTok permissions allow (no video.publish → draft to the app only).
          const modes = this.modesOf(a);
          if (!this.cfg.readonly && modes.length && !modes.includes(options.mode)) options.mode = modes[0];
        }
        if (a.network === "instagram" && !Array.isArray(options.collaborators)) options.collaborators = [];
        if (a.network === "youtube" && !Array.isArray(options.tags)) options.tags = [];
        if (a.network === "youtube" && t && options.title) this.ytTitleTouched[a.key] = true;
        return {
          format: fmt,
          text_override: t && t.text_override != null ? t.text_override : null,
          media_override: t && Array.isArray(t.media_override) ? t.media_override.slice() : null,
          options,
          auto_publish: t ? !!t.auto_publish : !!a.auto_publish_default,
        };
      },

      // ---------------------------------------------------------------- tracking & validation
      /** Called by x-effect: re-runs whenever anything the payload reads changes. */
      track() {
        const snap = this.snapshot();
        if (!this.ready || snap === priv.lastSnap) return;
        priv.lastSnap = snap;
        const dirty = snap !== priv.baseline;
        if (dirty !== this.dirty) this.dirty = dirty;
        if (!this.ro) {
          clearTimeout(priv.timer);
          priv.timer = setTimeout(() => this.validate(), 450);
        }
      },
      snapshot() {
        return JSON.stringify(this.payload("snapshot"));
      },
      rebase() {
        this.$nextTick(() => {
          priv.baseline = this.snapshot();
          priv.lastSnap = priv.baseline;
          this.dirty = false;
        });
      },
      async validate() {
        if (this.ro || !this.cfg.urls.validate) return;
        const seq = ++priv.seq;
        this.validating = true;
        try {
          const res = await App.fetchJSON(this.cfg.urls.validate, { method: "POST", body: this.payload(this.cfg.primary) });
          if (seq !== priv.seq) return;
          this.applyResult(res);
          this.validateFailed = false;
        } catch (e) {
          if (seq === priv.seq) this.validateFailed = true;
        } finally {
          if (seq === priv.seq) this.validating = false;
        }
      },
      applyResult(res) {
        if (!res) return;
        if (res.account_map && res.account_map.demo) this.accountMap.demo = res.account_map.demo;
        this.result = res;
      },
      visibleIssues(level) {
        const list = (this.result && (level === "error" ? this.result.errors : this.result.warnings)) || [];
        // A pristine composer with no network chosen yet does not shout "choose a network".
        const quiet = !this.dirty && !this.selected.length;
        return list.filter((it) => (it.account_key ? this.selected.includes(it.account_key) : !(quiet && it.code === "no_targets")));
      },
      errors() {
        return this.visibleIssues("error");
      },
      warnings() {
        return this.visibleIssues("warning");
      },
      issuesTitle(level) {
        const n = this.visibleIssues(level).length;
        if (level === "error") return n === 1 ? "1 error" : n + " errores";
        return n === 1 ? "1 aviso" : n + " avisos";
      },
      issuesFor(key, field) {
        const all = this.errors().concat(this.warnings());
        return all.filter((it) => it.account_key === key && (!field || it.field === field));
      },
      fieldError(key, field) {
        const it = this.errors().find((i) => i.account_key === key && i.field === field);
        return it ? it.message : "";
      },
      sectionHasError(key) {
        return this.errors().some((i) => i.account_key === key);
      },
      focusIssue(it) {
        if (it.account_key) {
          this.secOpen[it.account_key] = true;
          if (it.field === "text" || it.field === "media") {
            if (this.perNetwork && this.targets[it.account_key] && this.hasOverride(it.account_key)) this.tab = it.account_key;
            this.$nextTick(() => this.$refs.text && this.$refs.text.focus());
            return;
          }
          this.$nextTick(() => {
            // Not this.$el: called from @click on an issue button, $el is that button, not the composer.
            const scope = this.$refs.scroll || this.$root;
            const sec = scope && scope.querySelector('[data-section="' + it.account_key + '"]');
            if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
            const field = sec && sec.querySelector('[data-field="' + it.field + '"]');
            if (field) {
              const ctl = field.querySelector("input:not([type=hidden]), select, textarea, button") || field;
              setTimeout(() => ctl.focus({ preventScroll: true }), 250);
            }
          });
          return;
        }
        if (it.field === "scheduled_at") this.pop = "date";
        else if (it.field === "networks") {
          const row = this.$refs.nets;
          if (row) {
            row.classList.remove("is-flash");
            void row.offsetWidth;
            row.classList.add("is-flash");
          }
        } else if (this.$refs.text) this.$refs.text.focus();
      },

      // ---------------------------------------------------------------- accounts & formats
      ordered(keys) {
        const set = new Set(keys);
        return this.accounts.map((a) => a.key).filter((k) => set.has(k));
      },
      isSel(k) {
        return this.selected.includes(k);
      },
      net(k) {
        return (this.acc[k] || {}).network;
      },
      fmt(k) {
        return (this.targets[k] || {}).format;
      },
      formatSpec(a, key) {
        return ((a && a.formats) || []).find((f) => f.key === key);
      },
      formatLabel(k) {
        return FORMAT_LABELS_UP[this.fmt(k)] || String(this.fmt(k) || "").toUpperCase();
      },
      hasFormatChoice(k) {
        return ((this.acc[k] || {}).formats || []).length > 1;
      },
      toggleNet(k) {
        if (this.ro) return;
        if (this.isSel(k)) {
          this.selected = this.selected.filter((x) => x !== k);
          if (this.tab === k) this.tab = "common";
          if (this.pvKey === k) this.pvKey = this.selected[0] || null;
          if (this.selected.length < 2) this.perNetwork = false;
        } else {
          const a = this.acc[k];
          if (a && a.status === "disconnected") {
            toast("Esta cuenta está desconectada. Vuelve a conectarla en «Cuentas conectadas».", "warning");
            return;
          }
          this.selected = this.ordered(this.selected.concat([k]));
          this.secOpen[k] = true;
          this.pvKey = k;
          this.onSelected(k);
        }
        storage.set("composer.networks." + (this.cfg.brand && this.cfg.brand.id), this.selected);
      },
      setFormat(k, key) {
        const a = this.acc[k];
        const spec = this.formatSpec(a, key);
        if (!spec || spec.disabled) {
          if (spec && spec.disabled) toast(spec.disabled, "info");
          return;
        }
        const t = this.targets[k];
        const defaults = clone(spec.defaults || {});
        const kept = {};
        Object.keys(t.options || {}).forEach((opt) => {
          if (opt in defaults || opt === "consent" || opt === "commercial_content" || opt.startsWith("demo_")) kept[opt] = t.options[opt];
        });
        t.options = Object.assign(defaults, kept);
        t.format = key;
        this.pvKey = k;
        if (this.net(k) === "youtube") this.syncYtTitle(k);
      },
      onSelected(k) {
        const network = this.net(k);
        if (network === "tiktok") this.loadCreator(k);
        if (network === "youtube") {
          this.loadYt(k);
          this.syncYtTitle(k);
        }
      },
      accountId(k) {
        if (k === "demo") return this.accountMap.demo || "demo";
        return Number(k);
      },
      urlFor(name, k) {
        return (this.cfg.urls[name] || "").replace("{id}", encodeURIComponent(this.accountId(k)));
      },
      avatarLetter(k) {
        const a = this.acc[k] || {};
        return String(a.name || a.network_label || "?").trim().charAt(0).toUpperCase();
      },
      handle(k) {
        const a = this.acc[k] || {};
        const creator = this.net(k) === "tiktok" && this.creatorInfo(k);
        if (creator && creator.username) return creator.username;
        return a.username || (a.name || "").toLowerCase().replace(/\s+/g, "");
      },
      displayName(k) {
        const a = this.acc[k] || {};
        const creator = this.net(k) === "tiktok" && this.creatorInfo(k);
        return (creator && creator.nickname) || a.name || a.network_label;
      },

      // ---------------------------------------------------------------- text & per-network overrides
      get editorText() {
        if (this.tab === "common" || !this.targets[this.tab]) return this.text;
        const t = this.targets[this.tab];
        return t.text_override != null ? t.text_override : this.text;
      },
      set editorText(value) {
        if (this.tab === "common" || !this.targets[this.tab]) this.text = value;
        else this.targets[this.tab].text_override = value === this.text ? null : value;
        this.selected.forEach((k) => this.net(k) === "youtube" && this.syncYtTitle(k));
      },
      effectiveText(k) {
        const t = this.targets[k];
        return t && t.text_override != null ? t.text_override : this.text;
      },
      hasOverride(k) {
        const t = this.targets[k];
        if (!t) return false;
        const ids = this.media.map((m) => m.id);
        const mediaDiffers = t.media_override != null && JSON.stringify(t.media_override) !== JSON.stringify(ids);
        return (t.text_override != null && t.text_override !== this.text) || mediaDiffers;
      },
      startPerNetwork() {
        this.perNetwork = true;
        this.tab = this.selected[0] || "common";
        if (this.tab !== "common") this.pvKey = this.tab;
        this.$nextTick(() => this.$refs.text && this.$refs.text.focus());
      },
      stopPerNetwork() {
        this.perNetwork = false;
        this.tab = "common";
      },
      selectTab(k) {
        this.tab = k;
        if (k !== "common") this.pvKey = k;
        this.$nextTick(() => this.autosize());
      },
      resetOverride(k) {
        const t = this.targets[k];
        if (!t) return;
        t.text_override = null;
        t.media_override = null;
        if (this.tab === k) this.tab = "common";
        this.$nextTick(() => this.autosize());
      },
      autosize() {
        const ta = this.$refs.text;
        if (!ta) return;
        ta.style.height = "auto";
        ta.style.height = Math.max(ta.scrollHeight, 150) + "px";
      },
      insertText(str, opts) {
        if (this.ro) return;
        const ta = this.$refs.text;
        const value = this.editorText || "";
        let start = value.length;
        let end = value.length;
        if (ta && typeof ta.selectionStart === "number" && doc.activeElement === ta) {
          start = ta.selectionStart;
          end = ta.selectionEnd;
        } else if (ta && priv.caret != null) {
          start = end = Math.min(priv.caret, value.length);
        }
        let insert = str;
        if (opts && opts.space && start > 0 && !/\s/.test(value.charAt(start - 1))) insert = " " + insert;
        this.editorText = value.slice(0, start) + insert + value.slice(end);
        const caret = start + insert.length;
        priv.caret = caret;
        this.$nextTick(() => {
          if (!ta) return;
          ta.focus({ preventScroll: true });
          ta.setSelectionRange(caret, caret);
          this.autosize();
        });
      },
      rememberCaret() {
        const ta = this.$refs.text;
        if (ta) priv.caret = ta.selectionStart;
      },

      // ---------------------------------------------------------------- counters
      counter() {
        let keys = this.tab === "common" ? this.selected.filter((k) => this.targets[k].text_override == null) : [this.tab];
        if (!keys.length) keys = this.selected.slice();
        let best = null;
        keys.forEach((k) => {
          const a = this.acc[k];
          const spec = this.formatSpec(a, this.fmt(k));
          if (!spec) return;
          const text = this.tab === "common" && this.targets[k].text_override == null ? this.text : this.effectiveText(k);
          const len = textLength(a.unit, this.tab === "common" ? this.text : text);
          if (!best || spec.limit < best.limit) best = { key: k, network: a.network, len, limit: spec.limit, unit: a.unit };
        });
        if (!best) return { key: null, network: null, len: textLength("chars", this.editorText), limit: null };
        return best;
      },
      counterText() {
        const c = this.counter();
        const n = App.formatNumber ? App.formatNumber : (x) => x;
        return c.limit ? n(c.len) + " / " + n(c.limit) : n(c.len);
      },
      counterTone() {
        const c = this.counter();
        if (!c.limit) return "";
        if (c.len > c.limit) return "is-over";
        if (c.len > c.limit * 0.9) return "is-near";
        return "";
      },
      counterTitle() {
        const c = this.counter();
        if (!c.limit) return "Caracteres";
        const unit = c.unit === "bytes" ? "bytes" : "caracteres";
        return "Límite más estricto entre las redes elegidas: " + c.limit + " " + unit + " (" + (this.acc[c.key] || {}).network_label + ")";
      },
      igKey() {
        return this.selected.find((k) => this.net(k) === "instagram") || null;
      },
      igHashtags() {
        const k = this.igKey();
        if (!k) return 0;
        return countHashtags(this.effectiveText(k));
      },

      // ---------------------------------------------------------------- toolbar helpers
      togglePop(name) {
        if (this.ro) return;
        this.rememberCaret();
        this.pop = this.pop === name ? null : name;
        if (name === "link" && this.pop === "link") {
          const fb = this.fbPostKey();
          this.linkUrl = fb ? this.targets[fb].options.link || "" : "";
          this.$nextTick(() => this.$refs.linkInput && this.$refs.linkInput.focus());
        }
      },
      closePop(name) {
        if (!name || this.pop === name) this.pop = null;
      },
      onRootKeydown(e) {
        if (e.key === "Escape" && this.pop) {
          e.preventDefault();
          this.pop = null;
          return;
        }
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !this.ro) {
          e.preventDefault();
          this.save(this.cfg.primary);
        }
      },
      emoji(ch) {
        this.insertText(ch);
      },
      hashtag(tag) {
        this.insertText(tag ? tag + " " : "#", { space: true });
        if (tag) this.pop = null;
      },
      fbPostKey() {
        return this.selected.find((k) => this.net(k) === "facebook" && this.fmt(k) === "post") || null;
      },
      applyLink(asCard) {
        const url = (this.linkUrl || "").trim();
        if (!url) return;
        if (!/^https?:\/\//i.test(url)) {
          toast("El enlace debe empezar por http:// o https://", "warning");
          return;
        }
        const fb = this.fbPostKey();
        if (asCard && fb) {
          this.targets[fb].options.link = url;
          toast("El enlace se mostrará como vista previa en Facebook.", "success");
        } else this.insertText(url, { space: true });
        this.pop = null;
      },
      clearFbLink() {
        const fb = this.fbPostKey();
        if (fb) this.targets[fb].options.link = "";
      },
      firstCommentFor(k) {
        const list = FIRST_COMMENT_FORMATS[this.net(k)];
        return !!list && list.includes(this.fmt(k));
      },
      firstCommentKeys() {
        return this.selected.filter((k) => this.firstCommentFor(k));
      },
      get commonFirstComment() {
        const keys = this.firstCommentKeys();
        if (!keys.length) return "";
        const values = keys.map((k) => this.targets[k].options.first_comment || "");
        return values.every((v) => v === values[0]) ? values[0] : "";
      },
      set commonFirstComment(value) {
        this.firstCommentKeys().forEach((k) => {
          if (this.net(k) === "youtube" && this.targets[k].options.made_for_kids === true) return;
          this.targets[k].options.first_comment = value;
        });
      },
      firstCommentMixed() {
        const keys = this.firstCommentKeys();
        const values = keys.map((k) => this.targets[k].options.first_comment || "");
        return values.some((v) => v !== values[0]);
      },
      locationKey() {
        return this.selected.find((k) => this.net(k) === "instagram" && ["post", "reel"].includes(this.fmt(k))) || null;
      },

      // ---------------------------------------------------------------- media
      itemsFor(ids) {
        return (ids || []).map((id) => this.mediaIndex[id]).filter(Boolean);
      },
      get editorMedia() {
        if (this.tab === "common" || !this.targets[this.tab]) return this.media;
        const t = this.targets[this.tab];
        return t.media_override != null ? this.itemsFor(t.media_override) : this.media;
      },
      effectiveMedia(k) {
        const t = this.targets[k];
        return t && t.media_override != null ? this.itemsFor(t.media_override) : this.media;
      },
      setMedia(tab, list) {
        list.forEach((m) => (this.mediaIndex[m.id] = m));
        if (tab === "common" || !this.targets[tab]) this.media = list;
        else {
          const ids = list.map((m) => m.id);
          const same = JSON.stringify(ids) === JSON.stringify(this.media.map((m) => m.id));
          this.targets[tab].media_override = same ? null : ids;
        }
      },
      currentMedia(tab) {
        if (tab === "common" || !this.targets[tab]) return this.media.slice();
        const t = this.targets[tab];
        return t.media_override != null ? this.itemsFor(t.media_override) : this.media.slice();
      },
      addMedia(items, tab) {
        const where = tab || this.tab;
        const list = this.currentMedia(where);
        (items || []).forEach((it) => {
          if (it && it.id != null && !list.some((m) => m.id === it.id)) list.push(it);
        });
        this.setMedia(where, list);
        if (this.pvKey == null && this.selected.length) this.pvKey = this.selected[0];
      },
      removeMedia(index) {
        const list = this.currentMedia(this.tab);
        list.splice(index, 1);
        this.setMedia(this.tab, list);
      },
      moveMedia(from, to) {
        const list = this.currentMedia(this.tab);
        if (to < 0 || to >= list.length || from === to) return;
        const [item] = list.splice(from, 1);
        list.splice(to, 0, item);
        this.setMedia(this.tab, list);
      },
      onThumbDragStart(e, index) {
        this.dragFrom = index;
        e.dataTransfer.effectAllowed = "move";
        try {
          e.dataTransfer.setData("text/plain", "media:" + index);
        } catch (err) {
          /* old browsers */
        }
      },
      onThumbDrop(e, index) {
        if (this.dragFrom == null) return;
        e.preventDefault();
        e.stopPropagation();
        this.moveMedia(this.dragFrom, index);
        this.dragFrom = null;
      },
      onBoxDragOver(e) {
        if (this.ro) return;
        const types = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
        if (types.includes("Files")) {
          e.preventDefault();
          this.dropping = true;
        }
      },
      onBoxDrop(e) {
        this.dropping = false;
        if (this.ro || !e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
        e.preventDefault();
        this.uploadFiles(e.dataTransfer.files);
      },
      openPicker(purpose, kind, multiple) {
        if (this.ro) return;
        this.pickerPurpose = purpose || "media";
        const url = this.cfg.urls.picker + "?multiple=" + (multiple === false ? 0 : 1) + "&kind=" + (kind || "any");
        const host = this.$refs.pickerHost;
        if (window.htmx && host) window.htmx.ajax("GET", url, { target: host, swap: "innerHTML" });
        else toast("No se ha podido abrir la biblioteca.", "error");
      },
      onMediaSelected(e) {
        const purpose = this.pickerPurpose;
        if (!purpose) return;
        this.pickerPurpose = null;
        const items = (e.detail && e.detail.items) || [];
        if (!items.length) return;
        if (purpose === "media") this.addMedia(items);
        else if (purpose.startsWith("yt-thumb:")) {
          const k = purpose.slice(9);
          const it = items[0];
          if (it.kind !== "image") {
            toast("La miniatura tiene que ser una imagen.", "warning");
            return;
          }
          this.mediaIndex[it.id] = it;
          this.targets[k].options.thumbnail_media_id = it.id;
        }
      },
      pickFiles() {
        if (!this.ro && this.$refs.file) this.$refs.file.click();
      },
      onFileInput(e) {
        const files = e.target.files;
        if (files && files.length) this.uploadFiles(files);
        e.target.value = "";
      },
      uploadFiles(fileList) {
        Array.from(fileList).forEach((file) => this.uploadFile(file));
      },
      uploadFile(file) {
        const tab = this.tab;
        const id = "u" + Date.now() + Math.random().toString(16).slice(2);
        this.uploads.push({ id, name: file.name, progress: 0, tab });
        const entry = () => this.uploads.find((u) => u.id === id);
        const done = () => (this.uploads = this.uploads.filter((u) => u.id !== id));
        const fd = new FormData();
        fd.append("csrf_token", App.csrfToken ? App.csrfToken() : "");
        fd.append("files", file, file.name);
        const xhr = new XMLHttpRequest();
        xhr.open("POST", this.cfg.urls.upload);
        xhr.setRequestHeader("Accept", "application/json");
        if (App.csrfToken) xhr.setRequestHeader("X-CSRF-Token", App.csrfToken());
        xhr.upload.onprogress = (ev) => {
          const u = entry();
          if (u && ev.lengthComputable) u.progress = Math.round((ev.loaded * 100) / ev.total);
        };
        xhr.onload = () => {
          done();
          let data = null;
          try {
            data = JSON.parse(xhr.responseText);
          } catch (err) {
            data = null;
          }
          if (!Array.isArray(data)) {
            const msg = App.errorMessage ? App.errorMessage(xhr.status, xhr.getResponseHeader("X-Error-Message"), xhr.getResponseHeader("Content-Type"), xhr.responseText) : "No se ha podido subir el archivo.";
            toast("«" + file.name + "»: " + msg, "error");
            return;
          }
          const ok = data.filter((r) => r.ok);
          data.filter((r) => !r.ok).forEach((r) => toast("«" + (r.name || file.name) + "»: " + (r.error || "no se ha podido subir."), "error"));
          if (ok.length) {
            this.addMedia(
              ok.map((r) => ({ id: r.id, kind: r.kind, name: r.name, thumb_url: r.thumb_url, url: r.url, width: r.width, height: r.height, duration_s: r.duration_s })),
              tab
            );
          }
        };
        xhr.onerror = () => {
          done();
          toast("No se ha podido subir «" + file.name + "». ¿Sigue abierta la aplicación?", "error");
        };
        xhr.send(fd);
      },
      thumbFor(id) {
        const m = this.mediaIndex[id];
        return m ? m.thumb_url : "";
      },
      firstVideo(k) {
        return this.effectiveMedia(k).find((m) => m.kind === "video") || null;
      },
      hasImages(k) {
        return this.effectiveMedia(k).some((m) => m.kind === "image");
      },
      hasVideo(k) {
        return this.effectiveMedia(k).some((m) => m.kind === "video");
      },
      duration,
      humanSeconds,

      // ---------------------------------------------------------------- date & time
      nowWall() {
        const base = parseWall(this.cfg.now_local) || new Date();
        return new Date(base.getTime() + (Date.now() - this.loadedAt));
      },
      todayWall() {
        return wallDate(this.nowWall());
      },
      hasDate() {
        return !!(this.date && this.time);
      },
      dateLabel() {
        if (!this.hasDate()) return "Sin fecha";
        const d = parseWall(this.date + "T" + this.time);
        return d.getUTCDate() + " " + MONTHS_SHORT[d.getUTCMonth()] + " " + d.getUTCFullYear() + " " + d.getUTCHours() + ":" + pad(d.getUTCMinutes());
      },
      dateLong() {
        if (!this.hasDate()) return "";
        const d = parseWall(this.date + "T" + this.time);
        return d.getUTCDate() + " de " + MONTHS[d.getUTCMonth()] + " de " + d.getUTCFullYear() + ", " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
      },
      isPast() {
        if (!this.hasDate()) return false;
        return parseWall(this.date + "T" + this.time).getTime() <= this.nowWall().getTime();
      },
      quickDate(kind) {
        const now = this.nowWall();
        let d;
        if (kind === "hour") {
          d = new Date(now.getTime() + 3600000);
          d.setUTCMinutes(Math.ceil(d.getUTCMinutes() / 5) * 5, 0, 0);
        } else {
          d = new Date(now.getTime() + DAY_MS);
          d.setUTCHours(kind === "evening" ? 18 : 9, 0, 0, 0);
        }
        this.date = wallDate(d);
        this.time = wallTime(d);
      },
      clearDate() {
        this.date = "";
        this.time = "";
        this.pop = null;
      },
      pvDate() {
        const d = this.hasDate() ? parseWall(this.date + "T" + this.time) : this.nowWall();
        return d.getUTCDate() + " de " + MONTHS[d.getUTCMonth()];
      },
      pvTime() {
        const d = this.hasDate() ? parseWall(this.date + "T" + this.time) : this.nowWall();
        return d.getUTCHours() + ":" + pad(d.getUTCMinutes());
      },

      // ---------------------------------------------------------------- auto publish
      allAuto() {
        return this.selected.length > 0 && this.selected.every((k) => this.targets[k].auto_publish);
      },
      mixedAuto() {
        const vals = this.selected.map((k) => !!this.targets[k].auto_publish);
        return vals.includes(true) && vals.includes(false);
      },
      setAllAuto(on) {
        this.selected.forEach((k) => (this.targets[k].auto_publish = !!on));
      },
      autoHelp(k) {
        const t = this.targets[k];
        if (this.net(k) === "tiktok" && t.options.mode === "inbox") return "El vídeo llegará a la app de TikTok como borrador para que lo termines allí.";
        if (t.auto_publish) return "Se publicará automáticamente a la hora indicada.";
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
      ttCan(k, mode) {
        return this.ttModes(k).includes(mode);
      },
      ttModeLocked(k) {
        return this.ttModes(k).length < 2;
      },
      ttModeNote(k) {
        const tt = (this.acc[k] || {}).tiktok;
        return (tt && tt.note) || "";
      },
      async loadCreator(k) {
        if (k === "demo" && !this.accountMap.demo) return;
        if (!this.ttCan(k, "direct")) return; // creator_info belongs to Direct Post (video.publish)
        const id = this.accountId(k);
        if (!id || id === "demo") return;
        const wasClean = this.ready && !this.dirty;
        this.creator[k] = Object.assign({}, this.creator[k] || {}, { loading: true, error: null });
        try {
          const res = await App.fetchJSON(this.urlFor("creator_info", k));
          this.creator[k] = { loading: false, info: res.creator_info || null, error: res.ok ? null : res.message || "No se ha podido consultar TikTok.", demo: !!res.demo };
        } catch (e) {
          this.creator[k] = { loading: false, info: (this.acc[k] || {}).creator_info || null, error: e.message || "No se ha podido consultar TikTok." };
        }
        const info = this.creatorInfo(k);
        const opts = this.targets[k].options;
        const choices = (info && info.privacy_level_options) || [];
        // TikTok's posting UX forbids any default privacy, even when only one option is offered (research 03
        // §4.2/§4.3): the user always picks it. A saved value the account no longer offers is cleared, so the
        // select shows the placeholder instead of silently displaying another option the model does not hold.
        if (choices.length && opts.privacy_level && !choices.includes(opts.privacy_level)) {
          const label = this.cfg.privacy_labels[opts.privacy_level] || opts.privacy_level;
          opts.privacy_level = null;
          this.notice[k] = "La visibilidad guardada («" + label + "») ya no está disponible en tu cuenta de TikTok: elige otra.";
        }
        if (info) {
          if (info.comment_disabled) opts.disable_comment = true;
          if (info.duet_disabled) opts.disable_duet = true;
          if (info.stitch_disabled) opts.disable_stitch = true;
        }
        if (wasClean || !this.ready) this.rebase();
      },
      creatorInfo(k) {
        const c = this.creator[k];
        return (c && c.info) || (this.acc[k] || {}).creator_info || null;
      },
      privacyChoices(k) {
        const info = this.creatorInfo(k);
        const opts = (info && info.privacy_level_options) || [];
        return opts.map((value) => ({ value, label: this.cfg.privacy_labels[value] || value }));
      },
      unaudited(k) {
        const info = this.creatorInfo(k);
        const opts = (info && info.privacy_level_options) || [];
        return opts.length === 1 && opts[0] === "SELF_ONLY";
      },
      creatorFlag(k, flag) {
        const info = this.creatorInfo(k);
        return !!(info && info[flag]);
      },
      allow(k, key) {
        return !this.targets[k].options[key];
      },
      setAllow(k, key, on) {
        this.targets[k].options[key] = !on;
      },
      setCommercial(k, on) {
        const o = this.targets[k].options;
        o.commercial_content = !!on;
        if (!on) {
          o.brand_organic_toggle = false;
          o.brand_content_toggle = false;
        }
      },
      setBranded(k, on) {
        const o = this.targets[k].options;
        o.brand_content_toggle = !!on;
        if (on && o.privacy_level === "SELF_ONLY") {
          o.privacy_level = null;
          this.notice[k] = "El contenido de marca no puede ser privado: elige otra visibilidad.";
        } else this.notice[k] = "";
      },
      commercialLabel(k) {
        const o = this.targets[k].options;
        if (o.brand_content_toggle) return "Tu vídeo se etiquetará como «Colaboración pagada».";
        if (o.brand_organic_toggle) return "Tu vídeo se etiquetará como «Contenido promocional».";
        return "Elige al menos una opción: «Tu marca», «Contenido de marca» o ambas.";
      },
      maxDuration(k) {
        const info = this.creatorInfo(k);
        return info && info.max_video_post_duration_sec ? info.max_video_post_duration_sec : null;
      },
      tooLong(k) {
        const max = this.maxDuration(k);
        const v = this.firstVideo(k);
        return !!(max && v && v.duration_s && v.duration_s > max + 0.05);
      },
      coverMax(k) {
        const v = this.firstVideo(k);
        return v && v.duration_s ? Math.floor(v.duration_s * 1000) : 0;
      },
      coverLabel(k, key) {
        const ms = Number(this.targets[k].options[key] || 0);
        return (ms / 1000).toFixed(1).replace(".", ",") + " s";
      },

      // ---------------------------------------------------------------- Instagram
      addChip(k, key, max) {
        const raw = (this.drafts[k + key] || "").trim().replace(/^[@#]/, "");
        if (!raw) return;
        const list = this.targets[k].options[key] || [];
        const sep = key === "tags" ? /\s*,\s*/ : /[\s,]+/;
        const values = raw.split(sep).map((v) => v.trim().replace(/^[@#]/, "")).filter(Boolean);
        for (const v of values) {
          if (max && list.length >= max) {
            toast(key === "collaborators" ? "Instagram admite como máximo " + max + " colaboradores." : "Has alcanzado el máximo.", "warning");
            break;
          }
          if (!list.includes(v)) list.push(v);
        }
        this.targets[k].options[key] = list.slice();
        this.drafts[k + key] = "";
      },
      removeChip(k, key, index) {
        const list = (this.targets[k].options[key] || []).slice();
        list.splice(index, 1);
        this.targets[k].options[key] = list;
      },
      chipKey(e, k, key, max) {
        if (e.key === "Enter" || e.key === ",") {
          e.preventDefault();
          this.addChip(k, key, max);
        } else if (e.key === "Backspace" && !(this.drafts[k + key] || "")) {
          const list = this.targets[k].options[key] || [];
          if (list.length) this.removeChip(k, key, list.length - 1);
        }
      },

      // ---------------------------------------------------------------- YouTube
      async loadYt(k) {
        const id = this.accountId(k);
        if (!id || id === "demo") return;
        const get = async (name, store) => {
          if (this[store][k] && this[store][k].items) return;
          this[store][k] = { loading: true, items: [] };
          try {
            const res = await App.fetchJSON(this.urlFor(name, k));
            this[store][k] = { loading: false, items: res.items || [], error: res.ok ? null : res.message };
          } catch (e) {
            this[store][k] = { loading: false, items: [], error: e.message };
          }
        };
        await Promise.all([get("yt_categories", "ytCats"), get("yt_playlists", "ytLists")]);
      },
      syncYtTitle(k) {
        if (this.ytTitleTouched[k] || !this.targets[k]) return;
        this.targets[k].options.title = firstLine(this.effectiveText(k));
      },
      ytCategoryChoices(k) {
        const loaded = this.ytCats[k] && this.ytCats[k].items;
        return loaded && loaded.length ? loaded : this.cfg.yt_categories_default || [];
      },
      ytTitle(k) {
        const o = (this.targets[k] || {}).options || {};
        return o.title || firstLine(this.effectiveText(k)) || "Título del vídeo";
      },
      tagsLength(k) {
        const tags = (this.targets[k].options.tags || []).filter(Boolean);
        if (!tags.length) return 0;
        return tags.reduce((n, t) => n + t.length + (/\s/.test(t) ? 2 : 0), 0) + tags.length - 1;
      },
      setKids(k, value) {
        const o = this.targets[k].options;
        o.made_for_kids = value;
        if (value === true && o.first_comment) {
          o.first_comment = "";
          toast("Los vídeos para niños no admiten comentarios: se ha quitado el primer comentario.", "info");
        }
      },

      // ---------------------------------------------------------------- payload & save
      finalOptions(k) {
        return clone(this.targets[k].options);
      },
      payload(action) {
        return {
          action,
          layer: this.cfg.layer || "overlay",
          text: this.text,
          media_ids: this.media.map((m) => m.id),
          scheduled_at: this.hasDate() ? this.date + "T" + this.time : null,
          notes: this.notes,
          idea_stage: this.ideaStage,
          targets: this.selected.map((k) => {
            const t = this.targets[k];
            return {
              account_id: this.accountId(k),
              format: t.format,
              text_override: t.text_override,
              media_override: t.media_override,
              options: this.finalOptions(k),
              auto_publish: !!t.auto_publish,
            };
          }),
        };
      },
      canSchedule() {
        if (this.ro || this.saving || !this.selected.length || !this.hasDate() || this.isPast()) return false;
        return !!(this.result && this.result.can_schedule) && this.errors().length === 0;
      },
      canPublishNow() {
        if (this.ro || this.saving || !this.selected.length) return false;
        return !!(this.result && this.result.can_publish_now);
      },
      canPrimary() {
        if (this.cfg.primary === "idea") return !this.ro && !this.saving;
        return this.canSchedule();
      },
      primaryLabel() {
        if (this.cfg.primary === "idea") return this.cfg.mode === "edit" ? "Guardar idea" : "Guardar como idea";
        return "Programar";
      },
      primaryTitle() {
        if (this.cfg.primary === "idea" || this.canSchedule()) return "";
        if (!this.selected.length) return "Selecciona al menos una red social";
        if (!this.hasDate()) return "Elige la fecha y la hora";
        if (this.isPast()) return "La fecha de publicación está en el pasado";
        return this.errors().length ? "Corrige los errores para programar" : "Comprobando…";
      },
      async save(action) {
        if (this.saving || this.ro) return;
        if (action === "schedule" && !this.canSchedule()) {
          const first = this.errors()[0];
          toast(first ? first.message : this.primaryTitle() || "Revisa la publicación antes de programarla.", "warning");
          this.issuesOpen.error = true;
          return;
        }
        if (action === "publish_now") {
          const names = this.selected.map((k) => (this.acc[k] || {}).network_label).join(", ");
          const ok = await App.confirm("Se enviará ahora mismo a: " + names + ".", { title: "¿Publicar ahora?", ok: "Publicar ahora" });
          if (!ok) return;
        }
        this.pop = null;
        this.saving = action;
        let resp = null;
        let data = null;
        try {
          resp = await fetch(this.cfg.urls.save, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": App.csrfToken ? App.csrfToken() : "" },
            body: JSON.stringify(this.payload(action)),
          });
          data = await resp.json().catch(() => null);
        } catch (e) {
          this.saving = false;
          toast("No se ha podido conectar con la aplicación. ¿Sigue abierta la ventana del servidor?", "error");
          return;
        }
        if (resp.ok && data && data.ok) {
          this.savedOK = true;
          this.dirty = false;
          if (data.account_map && data.account_map.demo) this.accountMap.demo = data.account_map.demo;
          if (this.cfg.layer === "page") {
            window.location.href = data.redirect || this.cfg.urls.back || "/planificacion";
            return;
          }
          this.saving = false;
          this.fireTriggers(resp.headers.get("HX-Trigger"), data);
          return;
        }
        this.saving = false;
        if (data && data.validation) {
          this.applyResult(data.validation);
          this.issuesOpen.error = true;
        }
        const msg = (data && (data.message || data.detail)) || (App.errorMessage ? App.errorMessage(resp.status, resp.headers.get("X-Error-Message"), "", "") : "No se ha podido guardar.");
        toast(msg, resp.status >= 500 ? "error" : "warning");
      },
      fireTriggers(header, data) {
        let events = {};
        try {
          events = JSON.parse(header || "{}");
        } catch (e) {
          events = {};
        }
        if (!header) events = { toast: { message: data.message, kind: "success" }, closeModal: true, "planner:refresh": true };
        const body = doc.body;
        Object.keys(events)
          .filter((name) => name !== "closeModal")
          .forEach((name) => body.dispatchEvent(new CustomEvent(name, { bubbles: true, detail: events[name] })));
        if (events.closeModal) {
          if (App.modal) App.modal.close({ force: true });
          else body.dispatchEvent(new CustomEvent("closeModal", { bubbles: true }));
        }
      },

      // ---------------------------------------------------------------- closing
      confirmDiscard() {
        if (!this.dirty || this.savedOK || this.ro) return Promise.resolve(true);
        return App.confirm("Si cierras ahora se perderán los cambios que no has guardado.", {
          title: "¿Descartar los cambios?",
          ok: "Descartar",
          cancel: "Seguir editando",
          danger: true,
        });
      },
      requestClose() {
        if (this.cfg.layer === "page") {
          this.confirmDiscard().then((ok) => {
            if (!ok) return;
            this.savedOK = true;
            if (window.history.length > 1 && doc.referrer && doc.referrer.indexOf(window.location.origin) === 0) window.history.back();
            else window.location.href = this.cfg.urls.back || "/planificacion";
          });
          return;
        }
        if (App.modal) App.modal.close();
      },
      onBeforeClose(e) {
        if (!this.dirty || this.savedOK || this.ro) return;
        e.preventDefault();
        this.confirmDiscard().then((ok) => {
          if (!ok) return;
          this.savedOK = true;
          App.modal.close({ force: true });
        });
      },

      // ---------------------------------------------------------------- previews
      pvKind(k) {
        const network = this.net(k);
        const f = this.fmt(k);
        if (!network) return "none";
        if ((PHONE_FORMATS[network] || []).includes(f)) return "phone";
        if (network === "instagram" && f === "post") {
          const media = this.effectiveMedia(k);
          if (media.length === 1 && media[0].kind === "video") return "phone"; // published as a reel
        }
        if (network === "facebook") return "fb";
        if (network === "instagram") return "ig";
        if (network === "youtube") return "yt";
        return "demo";
      },
      pvMedia(k) {
        return this.effectiveMedia(k);
      },
      pvFirst(k) {
        return this.effectiveMedia(k)[0] || null;
      },
      pvText(k) {
        return this.effectiveText(k);
      },
      pvRich(k, clamp) {
        const text = this.pvText(k);
        const limit = clamp || 0;
        if (limit && !this.pvExpanded && text.length > limit) {
          return richText(text.slice(0, limit).replace(/\s+\S*$/, ""), this.net(k)) + '… <span class="pv-more">Ver más</span>';
        }
        return richText(text, this.net(k));
      },
      pvBg(k) {
        const m = this.pvFirst(k);
        return m && m.thumb_url ? "background-image:url('" + String(m.thumb_url).replace(/'/g, "%27") + "')" : "";
      },
      pvCaption(k) {
        return richText(this.pvText(k), this.net(k));
      },
      pvShowsText(k) {
        return this.fmt(k) !== "story";
      },
      netIcon(network) {
        return network && App.icon ? App.icon("net-" + network) : "";
      },
    };
  }

  function register(Alpine) {
    if (register.done) return;
    register.done = true;
    Alpine.data("composer", component);
  }
  if (window.Alpine && window.Alpine.data) register(window.Alpine);
  doc.addEventListener("alpine:init", () => register(window.Alpine));

  // Exposed for tests / other modules.
  window.Composer = { textLength, countHashtags, richText, firstLine, EMOJI };
})();
