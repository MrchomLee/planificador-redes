/* Planificador — Planificación (Calendario · Lista · Ideas · Papelera).
 *
 * Alpine components (registered on alpine:init; this file loads before Alpine):
 *   plannerCalendar  FullCalendar Mes/Semana/Día with "Mejores horas" shading, filters, search, drag & drop
 *   planList         Lista: row selection, "Acciones en bloque", filter badge
 *   rangePicker      Lista: date range popover with presets
 *   ideasBoard       Ideas: Kanban drag & drop between columns + client-side search
 *   tzPicker         brand time zone dropdown (zones loaded on first open)
 *
 * Calendar time model (SPEC §15): FullCalendar runs with timeZone 'UTC' and the server sends brand wall times
 * without offset, so every Date inside the calendar is "UTC-floating": its UTC fields ARE the brand wall time.
 * "Now" is computed in the brand zone with Intl, so the today column and the now line follow the brand.
 *
 * Refresh contract: closing the modal (composer) or the drawer (post detail), or an HX-Trigger "posts:changed"
 * from any module, dispatches "planner:refresh" on <body>; every planner view reloads its data on it.
 */
(function () {
  "use strict";

  const doc = document;
  const App = window.App || (window.App = {});
  const $ = (sel, root) => (root || doc).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || doc).querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const toast = (msg, kind) => (App.toast ? App.toast(msg, kind) : null);
  const storage = {
    get: (key, fallback) => (App.storage ? App.storage.get(key, fallback) : fallback),
    set: (key, value) => App.storage && App.storage.set(key, value),
  };
  const norm = (s) =>
    String(s || "")
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase();

  const DAYS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
  const DAYS_SHORT = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
  const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
  const MONTHS_SHORT = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"];
  const VIEW_KEYS = { dayGridMonth: "mes", timeGridWeek: "semana", timeGridDay: "dia" };
  const HOUR = 3600000;
  const DAY = 86400000;

  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  const pad = (n) => String(n).padStart(2, "0");
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const isoMinute = (d) => d.toISOString().slice(0, 16);
  const isoSecond = (d) => d.toISOString().slice(0, 19);
  const addDays = (d, n) => new Date(d.getTime() + n * DAY);
  const shortDay = (d) => d.getUTCDate() + " " + MONTHS_SHORT[d.getUTCMonth()] + " " + d.getUTCFullYear();
  function parseDay(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || "");
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  }

  // ------------------------------------------------------------------ brand wall clock
  const formatters = {};
  function wallParts(tz) {
    if (!formatters[tz]) {
      formatters[tz] = new Intl.DateTimeFormat("en-GB", {
        timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
      });
    }
    const out = {};
    formatters[tz].formatToParts(new Date()).forEach((p) => (out[p.type] = p.value));
    return out;
  }
  /** Current brand wall time as a UTC-floating Date. */
  function wallNow(tz) {
    try {
      const p = wallParts(tz);
      return new Date(Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second));
    } catch (e) {
      const d = new Date();
      return new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    }
  }
  function tickClocks() {
    $$("[data-tz-clock]").forEach((el) => {
      try {
        const p = wallParts(el.getAttribute("data-tz-clock"));
        el.textContent = +p.hour % 24 + ":" + p.minute;
      } catch (e) {
        /* unknown zone in this browser: keep the server value */
      }
    });
  }

  function openLayer(url, target) {
    if (window.htmx) window.htmx.ajax("GET", url, { target: target, swap: "innerHTML" });
    else window.location.href = url;
  }

  // Thumbnails that fail to load fall back to the icon placeholder (capture: error does not bubble).
  doc.addEventListener(
    "error",
    (e) => {
      const img = e.target;
      if (!(img instanceof HTMLImageElement) || !img.classList.contains("js-thumb")) return;
      const box = img.closest(".pthumb, .cal-event__media");
      if (box) box.classList.add("is-broken");
      img.remove();
    },
    true
  );

  // Keyboard focus across refreshes: a refresh re-renders the list / board (htmx) or the calendar events
  // (FullCalendar) and replaces the focused element — typically the row or event whose drawer was just closed
  // (closeLayer gives focus back to it right before "layer:closed"). Remember how to find it and focus its new
  // copy once the refresh has landed, so keyboard users keep their place instead of landing on <body>.
  let pendingFocus = null;
  function rememberFocus() {
    const el = doc.activeElement;
    if (!App.focusLocator || !el || el.closest("#modal-root, #drawer-root, #confirm-root")) return;
    const loc = App.focusLocator(el);
    if (loc) pendingFocus = { el: el, loc: loc, until: Date.now() + 8000 };
  }
  function restoreFocus() {
    const p = pendingFocus;
    if (!p) return;
    if (Date.now() > p.until) {
      pendingFocus = null;
      return;
    }
    if (p.el.isConnected) return; // not replaced (yet)
    const active = doc.activeElement;
    if (active && active !== doc.body && active !== doc.documentElement) {
      pendingFocus = null; // the user has moved on
      return;
    }
    const next = App.findByLocator(p.loc);
    if (next) {
      pendingFocus = null;
      next.focus({ preventScroll: true });
    }
  }
  doc.addEventListener("htmx:afterSettle", restoreFocus);

  // One refresh signal for every planner view.
  function requestRefresh() {
    rememberFocus();
    if (doc.body) doc.body.dispatchEvent(new CustomEvent("planner:refresh", { bubbles: true }));
  }
  doc.addEventListener("layer:closed", (e) => {
    const id = e.target && e.target.id;
    if (id === "modal-root" || id === "drawer-root") requestRefresh();
  });
  ["posts:changed", "post:saved", "post:deleted"].forEach((name) => doc.addEventListener(name, requestRefresh));

  // Minimal dropdown behaviour for components that also hold their own state.
  function dropdownMixin() {
    return {
      open: false,
      toggle() {
        this.open ? this.close() : this.show();
      },
      show() {
        this.open = true;
      },
      close(focusButton) {
        if (!this.open) return;
        this.open = false;
        if (focusButton && this.$refs.button) this.$refs.button.focus();
      },
      onKeydown(e) {
        if (e.key === "Escape" && this.open) {
          e.preventDefault();
          e.stopPropagation();
          this.close(true);
        }
      },
    };
  }

  // ------------------------------------------------------------------ calendar helpers
  function rangeLabelFor(view) {
    const start = view.currentStart;
    const last = new Date(view.currentEnd.getTime() - 1);
    if (view.type === "dayGridMonth") return cap(MONTHS[start.getUTCMonth()]) + " " + start.getUTCFullYear();
    if (view.type === "timeGridDay") return cap(DAYS[start.getUTCDay()]) + ", " + shortDay(start);
    return shortDay(start) + " - " + shortDay(last);
  }

  function headerText(arg) {
    const d = arg.date;
    if (arg.view.type === "dayGridMonth") return cap(DAYS_SHORT[d.getUTCDay()]);
    if (arg.view.type === "timeGridDay") return cap(DAYS[d.getUTCDay()]) + " " + d.getUTCDate() + " de " + MONTHS[d.getUTCMonth()];
    return cap(DAYS[d.getUTCDay()]) + " " + d.getUTCDate();
  }

  function bestEvents(data, start, end) {
    const out = [];
    const levels = data.levels || [];
    const matrix = data.matrix || [];
    for (let t = start.getTime(); t < end.getTime(); t += HOUR) {
      const d = new Date(t);
      const dow = (d.getUTCDay() + 6) % 7; // matrix rows: Monday = 0
      const hour = d.getUTCHours();
      const level = (levels[dow] || [])[hour] || 1;
      const score = (matrix[dow] || [])[hour] || 0;
      out.push({
        start: isoSecond(d),
        end: isoSecond(new Date(t + HOUR)),
        display: "background",
        classNames: ["best-hour", "best-hour--" + level],
        extendedProps: { best: true, level: level, score: score, dow: dow, hour: hour },
      });
    }
    return out;
  }

  function eventHtml(arg) {
    const p = arg.event.extendedProps || {};
    const compact = arg.view.type === "dayGridMonth";
    const nets = (p.networks || []).map((n) => '<span class="cal-net">' + App.icon("net-" + n) + "</span>").join("");
    let media = "";
    if (!compact && p.thumb) {
      media =
        '<span class="cal-event__media"><img class="cal-event__thumb js-thumb" src="' + esc(p.thumb) + '" alt="" loading="lazy" decoding="async">' +
        (p.mediaKind === "video" ? '<span class="cal-event__play">' + App.spriteIcon("play", 10) + "</span>" : "") +
        "</span>";
    }
    return (
      '<div class="cal-event st-' + esc(p.status) + (compact ? " cal-event--compact" : "") + (p.movable ? " is-movable" : "") + '">' +
      media +
      '<div class="cal-event__main">' +
      '<div class="cal-event__meta"><span class="cal-event__nets">' + nets + '</span>' +
      '<span class="cal-event__time">' + esc(p.time) + "</span>" +
      '<span class="cal-event__status" aria-hidden="true">' + App.icon("status-" + p.status) + "</span></div>" +
      '<div class="cal-event__text">' + (p.text ? esc(p.text) : "<em>Sin texto</em>") + "</div>" +
      "</div></div>"
    );
  }

  // ------------------------------------------------------------------ Alpine components
  doc.addEventListener("alpine:init", () => {
    const Alpine = window.Alpine;

    // ---------------------------------------------------------------- Calendario
    Alpine.data("plannerCalendar", () => {
      let cal = null; // FullCalendar instance (kept out of Alpine's reactive proxies)
      let cfg = {};
      const bestCache = {};
      let dragging = false;

      return {
        view: "timeGridWeek",
        rangeLabel: "",
        todayLabel: "Esta semana",
        q: "",
        filters: { redes: [], estados: [] },
        best: "",
        bestInfo: null,
        loading: false,

        get filterCount() {
          return this.filters.redes.length + this.filters.estados.length;
        },

        init() {
          try {
            cfg = JSON.parse(($("#planner-config") || {}).textContent || "{}");
          } catch (e) {
            cfg = {};
          }
          const prefs = storage.get("planner.prefs", {}) || {};
          this.view = cfg.initialView || prefs.view || "timeGridWeek";
          if (!VIEW_KEYS[this.view]) this.view = "timeGridWeek";
          this.filters = {
            redes: Array.isArray(prefs.redes) ? prefs.redes : [],
            estados: Array.isArray(prefs.estados) ? prefs.estados : [],
          };
          this.best = typeof prefs.best === "string" ? prefs.best : cfg.defaultBest || "";
          if (this.best) this.loadBest(this.best).catch(() => {});
          this.$nextTick(() => this.mount());
        },

        savePrefs() {
          storage.set("planner.prefs", { view: this.view, best: this.best, redes: this.filters.redes, estados: this.filters.estados });
        },

        calHeight() {
          const el = this.$refs.calendar;
          const top = el ? el.getBoundingClientRect().top + window.scrollY : 240;
          return Math.max(540, Math.round(window.innerHeight - top - 20));
        },

        scrollTime() {
          const h = wallNow(cfg.tz).getUTCHours();
          return pad(Math.min(Math.max(h - 1, 0), 18)) + ":00:00";
        },

        mount() {
          const el = this.$refs.calendar;
          if (!el || !window.FullCalendar) return;
          const self = this;
          cal = new window.FullCalendar.Calendar(el, {
            locale: "es",
            timeZone: "UTC",
            firstDay: 1,
            initialView: this.view,
            initialDate: cfg.initialDate || undefined,
            headerToolbar: false,
            height: this.calHeight(),
            now: () => isoSecond(wallNow(cfg.tz)),
            nowIndicator: true,
            allDaySlot: false,
            slotDuration: "00:30:00",
            slotLabelInterval: "01:00",
            slotLabelFormat: { hour: "numeric", minute: "2-digit", hour12: false },
            scrollTime: this.scrollTime(),
            scrollTimeReset: false,
            defaultTimedEventDuration: "01:00",
            editable: false,
            eventDurationEditable: false,
            dayMaxEvents: 3,
            eventMaxStack: 4,
            stickyHeaderDates: true,
            fixedWeekCount: false,
            dayHeaderContent: headerText,
            eventOrder: "start,title",
            eventSources: [
              { id: "best", events: (info, ok) => self.fetchBest(info, ok) },
              { id: "posts", events: (info, ok, fail) => self.fetchPosts(info, ok, fail) },
            ],
            eventContent: (arg) => (arg.event.display === "background" ? { html: "" } : { html: eventHtml(arg) }),
            eventDidMount: (arg) => self.decorate(arg),
            eventClick: (info) => {
              info.jsEvent.preventDefault();
              const id = info.event.extendedProps.postId;
              if (id) openLayer((cfg.detailUrl || "/publicaciones/{id}").replace("{id}", id), "#drawer-root");
            },
            dateClick: (info) => self.onDateClick(info),
            eventAllow: (drop, ev) => self.allowDrop(drop, ev),
            eventDragStart: () => (dragging = true),
            eventDragStop: () => (dragging = false),
            eventDrop: (info) => self.onDrop(info),
            datesSet: (info) => self.onDatesSet(info),
            loading: (isLoading) => {
              self.loading = isLoading;
              if (!isLoading && pendingFocus) setTimeout(restoreFocus);
            },
          });
          cal.render();

          el.addEventListener("mousemove", (e) => this.onGridMove(e));
          el.addEventListener("mouseleave", () => this.hideTip());
          let resizeTimer = null;
          window.addEventListener("resize", () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => cal && cal.setOption("height", this.calHeight()), 120);
          });
          doc.body.addEventListener("planner:refresh", () => this.refetchPosts());
          // Statuses change in the background (publishing → published): refresh every minute while visible.
          setInterval(() => {
            if (doc.hidden || dragging || (App.modal && App.modal.isOpen && App.modal.isOpen())) return;
            rememberFocus();
            this.refetchPosts();
          }, 60000);
        },

        // -- data sources
        fetchPosts(info, ok, fail) {
          const params = new URLSearchParams();
          params.set("start", info.startStr);
          params.set("end", info.endStr);
          if (this.q.trim()) params.set("q", this.q.trim());
          this.filters.redes.forEach((r) => params.append("redes", r));
          this.filters.estados.forEach((s) => params.append("estados", s));
          App.fetchJSON((cfg.eventsUrl || "/api/planificacion/eventos") + "?" + params.toString())
            .then((data) => ok(Array.isArray(data) ? data : []))
            .catch((err) => {
              fail(err);
              toast(err.message || "No se ha podido cargar el calendario.", "error");
            });
        },

        loadBest(net) {
          if (!bestCache[net]) {
            bestCache[net] = App.fetchJSON((cfg.bestTimesUrl || "/api/planificacion/mejores-horas") + "?red=" + encodeURIComponent(net)).catch((err) => {
              delete bestCache[net];
              throw err;
            });
          }
          return bestCache[net].then((data) => {
            if (this.best === net) this.bestInfo = data;
            return data;
          });
        },

        fetchBest(info, ok) {
          const net = this.best;
          if (!net || this.view === "dayGridMonth") return ok([]);
          this.loadBest(net)
            .then((data) => ok(this.best === net ? bestEvents(data, info.start, info.end) : []))
            .catch((err) => {
              ok([]);
              toast(err.message || "No se han podido cargar las mejores horas.", "warning");
            });
        },

        refetchPosts() {
          const src = cal && cal.getEventSourceById("posts");
          if (src) src.refetch();
        },
        refetchBest() {
          const src = cal && cal.getEventSourceById("best");
          if (src) src.refetch();
        },

        // -- toolbar
        prev() {
          if (cal) cal.prev();
        },
        next() {
          if (cal) cal.next();
        },
        goToday() {
          if (cal) cal.today();
        },
        setView(view) {
          if (!VIEW_KEYS[view] || !cal) return;
          this.view = view;
          cal.changeView(view);
          this.refetchBest();
          this.savePrefs();
        },
        pickDate() {
          const input = this.$refs.datePicker;
          if (!input || !cal) return;
          input.value = isoDay(cal.getDate());
          try {
            input.showPicker();
          } catch (e) {
            input.focus();
            input.click();
          }
        },
        gotoDate(value) {
          if (value && cal) cal.gotoDate(value);
        },
        applyFilters() {
          this.savePrefs();
          this.refetchPosts();
        },
        clearFilters() {
          this.filters = { redes: [], estados: [] };
          this.applyFilters();
        },
        setBest(net) {
          this.best = net || "";
          this.bestInfo = null;
          this.hideTip();
          this.savePrefs();
          if (this.best) this.loadBest(this.best).catch(() => {});
          this.refetchBest();
        },
        bestLogo(size) {
          if (this.best) return App.icon("net-" + this.best);
          return App.spriteIcon("flame", size || 18);
        },

        // -- calendar callbacks
        onDatesSet(info) {
          const view = info.view;
          this.view = view.type;
          this.rangeLabel = rangeLabelFor(view);
          this.todayLabel = view.type === "dayGridMonth" ? "Este mes" : view.type === "timeGridDay" ? "Hoy" : "Esta semana";
          this.savePrefs();
          try {
            const url = new URL(window.location.href);
            url.searchParams.set("vista", VIEW_KEYS[view.type] || "semana");
            url.searchParams.set("fecha", isoDay(view.currentStart));
            window.history.replaceState(window.history.state, "", url);
          } catch (e) {
            /* ignore */
          }
        },

        decorate(arg) {
          const ev = arg.event;
          const el = arg.el;
          const p = ev.extendedProps || {};
          if (ev.display === "background") {
            el.dataset.level = p.level;
            el.dataset.score = p.score;
            el.dataset.dow = p.dow;
            el.dataset.hour = p.hour;
            return;
          }
          if (p.postId) el.dataset.postId = p.postId; // focus locator (App.findByLocator) after a refetch
          if (pendingFocus) setTimeout(restoreFocus);
          const labels = cfg.networkLabels || {};
          const nets = (p.networks || []).map((n) => labels[n] || n).join(", ");
          const parts = [p.time, p.statusLabel, nets, p.text].filter(Boolean);
          el.setAttribute("aria-label", parts.join(" · "));
          el.title = parts.join(" · ") + (p.movable ? "\nArrastra para cambiar la fecha o la hora." : "");
        },

        onDateClick(info) {
          const now = wallNow(cfg.tz);
          let wall;
          if (info.allDay) {
            const day = info.dateStr.slice(0, 10);
            if (day === isoDay(now)) {
              const next = new Date(now.getTime());
              next.setUTCMinutes(0, 0, 0);
              next.setUTCHours(next.getUTCHours() + 1);
              wall = isoMinute(next);
            } else {
              wall = day + "T10:00";
            }
          } else {
            wall = isoMinute(info.date);
          }
          if (wall <= isoMinute(now)) {
            toast("Esa hora ya ha pasado. Elige un hueco a partir de ahora para programar.", "info");
            return;
          }
          openLayer((cfg.composerUrl || "/publicaciones/nueva") + "?fecha=" + encodeURIComponent(wall), "#modal-root");
        },

        allowDrop(drop, ev) {
          if (!ev.extendedProps || !ev.extendedProps.movable) return false;
          const now = wallNow(cfg.tz).getTime();
          if (drop.allDay) {
            // Month view: the event keeps its time of day.
            const s = ev.start;
            const d = drop.start;
            const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), s.getUTCHours(), s.getUTCMinutes());
            return t > now;
          }
          return drop.start.getTime() > now;
        },

        onDrop(info) {
          const ev = info.event;
          const body = new FormData();
          body.append("start", isoMinute(ev.start));
          const url = (cfg.rescheduleUrl || "/api/planificacion/publicaciones/{id}/reprogramar").replace("{id}", ev.extendedProps.postId);
          App.fetchJSON(url, { method: "POST", body: body })
            .then((res) => {
              toast((res && res.message) || "Publicación movida.", "success");
              this.refetchPosts();
            })
            .catch((err) => {
              info.revert();
              toast(err.message || "No se ha podido mover la publicación.", "warning");
            });
        },

        // -- best-times tooltip
        onGridMove(e) {
          const bg = this.best && e.target && e.target.closest ? e.target.closest(".fc-bg-event.best-hour") : null;
          if (!bg) return this.hideTip();
          const tip = this.$refs.tip;
          if (!tip) return;
          const level = +bg.dataset.level || 1;
          const dow = +bg.dataset.dow || 0;
          const hour = +bg.dataset.hour || 0;
          const score = Math.round((+bg.dataset.score || 0) * 100);
          const labels = (this.bestInfo && this.bestInfo.levelLabels) || cfg.bestLevelLabels || [];
          const key = dow + ":" + hour + ":" + level;
          if (tip.dataset.key !== key) {
            tip.dataset.key = key;
            tip.innerHTML =
              "<strong>" + esc(cap(DAYS[(dow + 1) % 7])) + " · " + hour + ":00 – " + (hour + 1) + ":00</strong>" +
              '<span><i class="best-swatch--' + level + '"></i>' + esc(labels[level - 1] || "") + " · " + score + "/100</span>";
          }
          tip.hidden = false;
          const w = tip.offsetWidth;
          const h = tip.offsetHeight;
          let x = e.clientX + 14;
          let y = e.clientY + 16;
          if (x + w > window.innerWidth - 8) x = e.clientX - w - 14;
          if (y + h > window.innerHeight - 8) y = e.clientY - h - 12;
          tip.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px)";
        },
        hideTip() {
          const tip = this.$refs.tip;
          if (tip && !tip.hidden) tip.hidden = true;
        },
      };
    });

    // ---------------------------------------------------------------- Lista
    Alpine.data("planList", () => ({
      selected: 0,
      init() {
        this.sync();
        this.$el.addEventListener("htmx:afterSettle", () => this.sync());
        const form = $("#list-filters");
        if (form) form.addEventListener("change", () => this.syncFilterBadge());
      },
      onChange(e) {
        const t = e.target;
        if (t && t.matches && t.matches("[data-select-all]")) {
          $$('#plan-list input[name="ids"]').forEach((b) => (b.checked = t.checked));
        }
        this.sync();
      },
      sync() {
        const list = $("#plan-list");
        if (!list) return;
        const boxes = $$('input[name="ids"]', list);
        this.selected = boxes.filter((b) => b.checked).length;
        boxes.forEach((b) => {
          const row = b.closest("tr");
          if (row) row.classList.toggle("is-selected", b.checked);
        });
        const all = $("[data-select-all]", list);
        if (all) {
          all.checked = boxes.length > 0 && this.selected === boxes.length;
          all.indeterminate = this.selected > 0 && this.selected < boxes.length;
        }
      },
      syncFilterBadge() {
        const form = $("#list-filters");
        if (!form) return;
        const n = $$('input[name="redes"]:checked, input[name="estados"]:checked', form).length;
        const badge = $("[data-filter-count]", form);
        if (badge) {
          badge.textContent = n;
          badge.hidden = !n;
        }
        const btn = $("[data-filter-btn]", form);
        if (btn) btn.classList.toggle("is-on", n > 0);
      },
      clearFilters() {
        const form = $("#list-filters");
        if (!form) return;
        $$('input[name="redes"], input[name="estados"]', form).forEach((b) => (b.checked = false));
        form.dispatchEvent(new Event("change", { bubbles: true }));
      },
    }));

    Alpine.data("rangePicker", (desde, hasta, today) => ({
      ...dropdownMixin(),
      desde: desde,
      hasta: hasta,
      today: today,
      label: "",
      init() {
        this.sync();
      },
      sync() {
        const a = parseDay(this.desde);
        const b = parseDay(this.hasta);
        this.label = a && b ? shortDay(a) + " - " + shortDay(b) : "Elige un rango";
      },
      set(a, b) {
        this.desde = isoDay(a);
        this.hasta = isoDay(b);
        this.$refs.desde.value = this.desde;
        this.$refs.hasta.value = this.hasta;
        this.sync();
        this.$refs.hasta.dispatchEvent(new Event("change", { bubbles: true }));
        this.close(true);
      },
      preset(from, to) {
        const t = parseDay(this.today) || new Date();
        this.set(addDays(t, from), addDays(t, to));
      },
      presetMonth(offset) {
        const t = parseDay(this.today) || new Date();
        const a = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + offset, 1));
        const b = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + offset + 1, 0));
        this.set(a, b);
      },
    }));

    // ---------------------------------------------------------------- Ideas
    // Methods run from @input/@drop on inner elements, where this.$el is that element: query from this.$root.
    Alpine.data("ideasBoard", () => ({
      q: "",
      dragId: null,
      overCol: null,
      init() {
        // The board re-renders (htmx) after every change: keep the current search applied.
        this.$el.addEventListener("htmx:afterSettle", () => this.filter());
      },
      filter() {
        const q = norm(this.q.trim());
        $$(".idea-card", this.$root).forEach((card) => {
          card.hidden = !!q && !norm(card.dataset.search).includes(q);
        });
      },
      start(e, id) {
        this.dragId = String(id);
        try {
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", String(id));
        } catch (err) {
          /* old browsers */
        }
        const card = e.target.closest && e.target.closest(".idea-card");
        if (card) requestAnimationFrame(() => card.classList.add("is-dragging"));
      },
      end(e) {
        const card = e.target.closest && e.target.closest(".idea-card");
        if (card) card.classList.remove("is-dragging");
        this.dragId = null;
        this.overCol = null;
      },
      over(e, col) {
        if (!this.dragId) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
        this.overCol = col;
      },
      leave(e, col) {
        if (e.currentTarget.contains(e.relatedTarget)) return;
        if (this.overCol === col) this.overCol = null;
      },
      async drop(e, col) {
        e.preventDefault();
        const id = this.dragId || (e.dataTransfer && e.dataTransfer.getData("text/plain"));
        this.overCol = null;
        this.dragId = null;
        if (!id) return;
        const card = doc.getElementById("idea-" + id);
        if (!card) return;
        card.classList.remove("is-dragging");
        const from = card.closest("[data-stage]");
        if (!from || from.dataset.stage === col) return;
        const list = $('[data-stage="' + col + '"] .idea-col__list', this.$root);
        if (!list) return;
        const parent = card.parentNode;
        const nextSibling = card.nextSibling;
        list.prepend(card);
        this.recount();
        const body = new FormData();
        body.append("etapa", col);
        try {
          const res = await App.fetchJSON("/ideas/" + encodeURIComponent(id) + "/etapa", { method: "POST", body: body });
          toast((res && res.message) || "Idea movida.", "success");
          requestRefresh(); // re-render so the card menu offers the right columns
        } catch (err) {
          parent.insertBefore(card, nextSibling);
          this.recount();
          toast(err.message || "No se ha podido mover la idea.", "warning");
        }
      },
      recount() {
        $$("[data-stage]", this.$root).forEach((col) => {
          const n = $$(".idea-card", col).length;
          const count = $("[data-count]", col);
          if (count) count.textContent = n;
          col.classList.toggle("is-empty", !n);
        });
      },
    }));

    // ---------------------------------------------------------------- time zone picker
    Alpine.data("tzPicker", (current) => ({
      ...dropdownMixin(),
      current: current,
      query: "",
      zones: [],
      loading: false,
      loaded: false,
      load() {
        if (!this.open) return;
        this.$nextTick(() => this.$refs.search && this.$refs.search.focus());
        if (this.loaded || this.loading) return;
        this.loading = true;
        App.fetchJSON("/api/planificacion/zonas-horarias")
          .then((zones) => {
            this.zones = Array.isArray(zones) ? zones : [];
            this.loaded = true;
          })
          .catch((err) => toast(err.message || "No se han podido cargar las zonas horarias.", "error"))
          .finally(() => (this.loading = false));
      },
      filtered() {
        const q = norm(this.query.trim()).replace(/\s+/g, " ");
        if (!q) return this.zones;
        return this.zones.filter((z) => norm(z.label + " " + z.name + " " + z.offset).includes(q)).slice(0, 120);
      },
    }));
  });

  // ------------------------------------------------------------------ init
  function init() {
    tickClocks();
    setInterval(tickClocks, 15000);
  }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();
