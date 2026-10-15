/* Estadísticas — charts (Chart.js), shared HTML tooltip, heatmap hover, posts table sort, "Actualizar ahora".
 *
 * Data: <script type="application/json" id="an-data"> {"charts": {key: spec}} rendered by routes/analytics.py.
 * spec = {type: "line"|"bar", labels: [ISO dates], series: [{label, values, color, fill?, dates?, abs?}],
 *         stacked?, diverging?, integer?}
 * color: a categorical token name ("chart-1"…"chart-8") or "muted" (de-emphasised comparison series).
 * Every canvas is <canvas data-chart="key">; colours are read from the CSS tokens (light/dark) and the charts
 * are rebuilt on window "themechange" and OS theme changes. Charts follow the dataviz skill: thin marks, 2 px
 * lines, 4 px rounded bar ends anchored to the baseline, hairline solid grid, one axis, crosshair + tooltip.
 */
(function () {
  "use strict";

  const doc = document;
  const App = window.App || {};
  const MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic"];
  const DAYS = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
  const charts = [];
  let data = { charts: {} };

  // ------------------------------------------------------------------ helpers
  function css(name) {
    return getComputedStyle(doc.documentElement).getPropertyValue(name).trim();
  }
  function parseDay(iso) {
    const [y, m, d] = String(iso).split("-").map(Number);
    return new Date(y, (m || 1) - 1, d || 1);
  }
  function shortDate(iso) {
    const d = parseDay(iso);
    return d.getDate() + " " + MONTHS[d.getMonth()];
  }
  function longDate(iso) {
    const d = parseDay(iso);
    return DAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear();
  }
  const compactFmt = new Intl.NumberFormat("es-ES", { notation: "compact", maximumFractionDigits: 1 });
  function fmt(n, decimals) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    if (App.formatNumber) return App.formatNumber(n, decimals || 0);
    return Number(n).toLocaleString("es-ES", { maximumFractionDigits: decimals || 0 });
  }
  function alpha(hex, a) {
    const h = String(hex || "").replace("#", "");
    if (h.length !== 6) return hex;
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + a + ")";
  }
  function theme() {
    return {
      grid: css("--chart-grid") || "#e7e8ee",
      axis: css("--chart-axis") || "#c7cbd6",
      text: css("--chart-text") || "#5b6275",
      surface: css("--surface") || "#ffffff",
      muted: css("--text-subtle") || "#868d9f",
      font: css("--font-sans") || "system-ui, sans-serif",
    };
  }
  function seriesColor(name) {
    if (name === "muted") return css("--text-subtle") || "#868d9f";
    return css("--" + (name || "chart-1")) || "#2a78d6";
  }

  // ------------------------------------------------------------------ tooltip (shared)
  const tip = () => doc.getElementById("an-tip");

  function showTip(x, y, title, rows) {
    const el = tip();
    if (!el) return;
    el.textContent = "";
    if (title) {
      const t = doc.createElement("div");
      t.className = "an-tip__title";
      t.textContent = title;
      el.appendChild(t);
    }
    rows.forEach((r) => {
      const row = doc.createElement("div");
      row.className = "an-tip__row";
      if (r.color) {
        const key = doc.createElement("span");
        key.className = "an-tip__key" + (r.bar ? " an-tip__key--bar" : "");
        key.style.background = r.color;
        row.appendChild(key);
      }
      const val = doc.createElement("span");
      val.className = "an-tip__val";
      val.textContent = r.value;
      row.appendChild(val);
      if (r.name) {
        const name = doc.createElement("span");
        name.className = "an-tip__name";
        name.textContent = r.name;
        row.appendChild(name);
      }
      el.appendChild(row);
    });
    el.hidden = false;
    const w = el.offsetWidth, h = el.offsetHeight;
    let left = x + 14, top = y - h - 12;
    if (left + w > window.innerWidth - 8) left = x - w - 14;
    if (top < 8) top = y + 16;
    el.style.left = Math.max(8, left) + "px";
    el.style.top = Math.max(8, top) + "px";
  }
  function hideTip() {
    const el = tip();
    if (el) el.hidden = true;
  }

  function externalTooltip(spec) {
    return function (context) {
      const { chart, tooltip } = context;
      if (!tooltip || tooltip.opacity === 0 || !tooltip.dataPoints || !tooltip.dataPoints.length) {
        hideTip();
        return;
      }
      const idx = tooltip.dataPoints[0].dataIndex;
      const rows = [];
      chart.data.datasets.forEach((ds, i) => {
        const s = spec.series[i] || {};
        if (!chart.isDatasetVisible(i)) return;
        let v = ds.data[idx];
        if (v !== null && v !== undefined && s.abs) v = Math.abs(v);
        let name = s.label || ds.label;
        if (s.dates && s.dates[idx] && s.color === "muted") name += " (" + shortDate(s.dates[idx]) + ")";
        let value = fmt(v);
        if (spec.diverging && typeof v === "number" && v > 0) value = "+" + value;
        rows.push({ value: value, name: name, color: ds._anColor, bar: spec.type === "bar" });
      });
      if (spec.stacked && spec.type === "bar" && rows.length > 1) {
        const total = chart.data.datasets.reduce((acc, ds, i) => {
          const v = ds.data[idx];
          return acc + (typeof v === "number" && !(spec.series[i] || {}).abs ? v : 0);
        }, 0);
        rows.push({ value: fmt(total), name: "Total" });
      }
      const rect = chart.canvas.getBoundingClientRect();
      showTip(rect.left + tooltip.caretX, rect.top + tooltip.caretY, longDate(spec.labels[idx]), rows);
    };
  }

  // Vertical hairline at the hovered X (line charts): readers aim at a date, not at a 2 px line.
  const crosshair = {
    id: "anCrosshair",
    afterDatasetsDraw(chart) {
      const active = chart.tooltip && chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
      if (!active.length || chart.config.type !== "line") return;
      const x = active[0].element.x;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, top);
      ctx.lineTo(Math.round(x) + 0.5, bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = theme().axis;
      ctx.stroke();
      ctx.restore();
    },
  };

  // ------------------------------------------------------------------ chart builders
  function isolatedPoints(values) {
    return values.map((v, i) => v !== null && v !== undefined && (values[i - 1] == null) && (values[i + 1] == null));
  }

  function buildConfig(spec) {
    const t = theme();
    const isLine = spec.type === "line";
    const labels = spec.labels || [];
    const datasets = (spec.series || []).map((s, i) => {
      const c = seriesColor(s.color);
      const values = (s.values || []).map((v) => (v === null || v === undefined ? null : Number(v)));
      const ds = { label: s.label, data: values, _anColor: c };
      if (isLine) {
        const iso = isolatedPoints(values);
        const prev = s.color === "muted";
        Object.assign(ds, {
          borderColor: c,
          backgroundColor: s.fill ? alpha(c, 0.1) : c,
          fill: s.fill ? "origin" : false,
          borderWidth: prev ? 1.5 : 2,
          borderCapStyle: "round",
          borderJoinStyle: "round",
          tension: 0.3,
          cubicInterpolationMode: "monotone",
          spanGaps: true,
          pointRadius: values.map((_, j) => (iso[j] ? 3 : 0)),
          pointBackgroundColor: c,
          pointHoverRadius: 5,
          pointHitRadius: 12,
          pointHoverBackgroundColor: c,
          pointHoverBorderColor: t.surface,
          pointHoverBorderWidth: 2,
          order: prev ? 2 : 1,
        });
      } else {
        const diverging = !!spec.diverging;
        const neg = seriesColor("chart-8");
        Object.assign(ds, {
          backgroundColor: diverging ? values.map((v) => (v !== null && v < 0 ? neg : c)) : c,
          hoverBackgroundColor: diverging ? values.map((v) => alpha(v !== null && v < 0 ? neg : c, 0.78)) : alpha(c, 0.78),
          borderSkipped: "start",
          borderRadius: spec.stacked ? stackRadius(spec, i) : 4,
          borderColor: t.surface,
          borderWidth: spec.stacked && (spec.series || []).length > 1 ? stackGap(spec, i) : 0,
          maxBarThickness: 24,
          categoryPercentage: 0.82,
          barPercentage: 0.92,
          stack: spec.stacked ? "s" : undefined,
        });
      }
      return ds;
    });

    return {
      type: isLine ? "line" : "bar",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 260 },
        normalized: true,
        interaction: { mode: "index", intersect: false, axis: "x" },
        layout: { padding: { top: 6, right: 6 } },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false, external: externalTooltip(spec) },
        },
        scales: {
          x: {
            stacked: !!spec.stacked,
            grid: { display: false },
            border: { color: t.axis, width: 1 },
            ticks: {
              color: t.text,
              font: { family: t.font, size: 12 },
              maxRotation: 0,
              autoSkip: true,
              autoSkipPadding: 18,
              maxTicksLimit: 10,
              callback: (_v, i) => shortDate(labels[i]),
            },
          },
          y: {
            stacked: !!spec.stacked,
            beginAtZero: !isLine || !looksLikeStock(spec),
            grace: isLine ? "8%" : 0,
            grid: {
              color: (ctx) => (ctx.tick && ctx.tick.value === 0 && !isLine ? t.axis : t.grid),
              lineWidth: 1,
              drawTicks: false,
            },
            border: { display: false },
            ticks: {
              color: t.text,
              font: { family: t.font, size: 12 },
              padding: 8,
              maxTicksLimit: spec.type === "bar" && spec.integer ? 4 : 5,
              precision: spec.integer ? 0 : undefined,
              callback: (v) => (Math.abs(v) >= 10000 ? compactFmt.format(v) : fmt(v, Number.isInteger(v) ? 0 : 1)),
            },
          },
        },
      },
      plugins: [crosshair],
    };
  }

  // Stock series (followers, totals) should not be forced to start at zero: their change is the story.
  function looksLikeStock(spec) {
    return !!spec.stock;
  }

  // Only the outer segment of a stack carries the 4 px rounded end.
  function stackRadius(spec, i) {
    return (ctx) => {
      const idx = ctx.dataIndex;
      const v = ctx.raw;
      if (v === null || v === undefined || v === 0) return 0;
      const series = spec.series || [];
      for (let j = series.length - 1; j > i; j--) {
        const other = Number((series[j].values || [])[idx]);
        if (other && Math.sign(other) === Math.sign(v)) return 0;
      }
      return 4;
    };
  }
  // 2 px surface gap between stacked segments (on the side facing the next segment).
  function stackGap(spec, i) {
    return (ctx) => {
      const v = ctx.raw;
      if (!v) return 0;
      const series = spec.series || [];
      for (let j = i + 1; j < series.length; j++) {
        const other = Number((series[j].values || [])[ctx.dataIndex]);
        if (other && Math.sign(other) === Math.sign(v)) return v > 0 ? { top: 2, bottom: 0, left: 0, right: 0 } : { bottom: 2, top: 0, left: 0, right: 0 };
      }
      return 0;
    };
  }

  function hasValues(spec) {
    return (spec.series || []).some((s) => (s.values || []).some((v) => v !== null && v !== undefined && v !== 0));
  }

  function mount(canvas) {
    const key = canvas.dataset.chart;
    const spec = (data.charts || {})[key];
    const box = canvas.parentElement;
    if (!spec || !window.Chart) return;
    if (!hasValues(spec)) box.classList.add("is-empty");
    const chart = new window.Chart(canvas, buildConfig(spec));
    chart._anSpec = spec;
    charts.push(chart);
    canvas.tabIndex = 0;
    canvas.addEventListener("mouseleave", hideTip);
    canvas.addEventListener("blur", () => {
      chart.setActiveElements([]);
      chart.tooltip.setActiveElements([], { x: 0, y: 0 });
      chart.update("none");
      hideTip();
    });
    // Keyboard: ←/→ walk through the days with the same readout as hover.
    canvas.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Home" && e.key !== "End") return;
      e.preventDefault();
      const n = (spec.labels || []).length;
      if (!n) return;
      const cur = chart._anIndex === undefined ? n - 1 : chart._anIndex;
      let next = cur;
      if (e.key === "ArrowLeft") next = Math.max(0, cur - 1);
      if (e.key === "ArrowRight") next = Math.min(n - 1, cur + 1);
      if (e.key === "Home") next = 0;
      if (e.key === "End") next = n - 1;
      focusIndex(chart, next);
    });
    canvas.addEventListener("focus", () => focusIndex(chart, chart._anIndex === undefined ? (spec.labels || []).length - 1 : chart._anIndex));
  }

  function focusIndex(chart, idx) {
    chart._anIndex = idx;
    const active = chart.data.datasets.map((_, i) => ({ datasetIndex: i, index: idx })).filter((a) => chart.isDatasetVisible(a.datasetIndex));
    const meta = chart.getDatasetMeta(0);
    const el = meta && meta.data[idx];
    chart.setActiveElements(active);
    chart.tooltip.setActiveElements(active, el ? { x: el.x, y: el.y } : { x: 0, y: 0 });
    chart.update("none");
  }

  function mountAll() {
    while (charts.length) {
      const c = charts.pop();
      try {
        c.destroy();
      } catch (e) {
        /* ignore */
      }
    }
    doc.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
      canvas.parentElement.classList.remove("is-empty");
      mount(canvas);
    });
  }

  // ------------------------------------------------------------------ heatmap hover
  function initHeatmap() {
    doc.querySelectorAll(".an-heat").forEach((grid) => {
      grid.addEventListener("pointerover", (e) => {
        const cell = e.target.closest(".an-heat__cell");
        if (!cell) return;
        const r = cell.getBoundingClientRect();
        showTip(r.left + r.width / 2, r.top, cell.dataset.tipTitle, [
          { value: cell.dataset.tipValue, name: "respecto a la mejor franja", color: getComputedStyle(cell).backgroundColor, bar: true },
        ]);
      });
      grid.addEventListener("pointerleave", hideTip);
    });
  }

  // ------------------------------------------------------------------ posts table sort
  function initSort() {
    doc.querySelectorAll("table[data-an-sortable]").forEach((table) => {
      const tbody = table.tBodies[0];
      table.querySelectorAll("th[data-sort-key] button").forEach((btn) => {
        btn.addEventListener("click", () => {
          const th = btn.closest("th");
          const key = th.dataset.sortKey;
          const textual = key === "type";
          const current = th.getAttribute("aria-sort");
          let dir;
          if (current === "descending") dir = "ascending";
          else if (current === "ascending") dir = "descending";
          else dir = textual ? "ascending" : "descending";
          table.querySelectorAll("th[aria-sort]").forEach((h) => h.setAttribute("aria-sort", "none"));
          th.setAttribute("aria-sort", dir);
          const attr = "data-" + key.replace(/_/g, "-");
          const rows = Array.from(tbody.rows);
          const sign = dir === "ascending" ? 1 : -1;
          rows.sort((a, b) => {
            const va = a.getAttribute(attr) || "";
            const vb = b.getAttribute(attr) || "";
            if (va === "" && vb !== "") return 1; // empty values always last
            if (vb === "" && va !== "") return -1;
            if (key === "date" || textual) return sign * va.localeCompare(vb, "es");
            return sign * (parseFloat(va) - parseFloat(vb));
          });
          rows.forEach((r) => tbody.appendChild(r));
        });
      });
    });
  }

  // ------------------------------------------------------------------ "Actualizar ahora"
  const root = () => doc.querySelector("[data-an-refresh-root]");
  const REFRESH_URL = "/estadisticas/actualizar";
  const STATUS_URL = "/estadisticas/estado";

  function setBusy(busy) {
    doc.querySelectorAll("[data-an-refresh]").forEach((b) => {
      b.classList.toggle("is-busy", busy);
      if (busy) b.setAttribute("aria-busy", "true");
      else b.removeAttribute("aria-busy");
      b.disabled = busy;
    });
  }

  function poll(delay) {
    setTimeout(async () => {
      let s;
      try {
        s = await App.fetchJSON(STATUS_URL);
      } catch (e) {
        setBusy(false);
        App.toast && App.toast(e.message, "error");
        return;
      }
      if (s.running) return poll(1500);
      setBusy(false);
      if (App.toast) App.toast(s.message || "Estadísticas actualizadas.", s.kind || "success");
      setTimeout(() => window.location.reload(), 900);
    }, delay);
  }

  async function refresh() {
    setBusy(true);
    try {
      const r = await App.fetchJSON(REFRESH_URL, { method: "POST" });
      if (App.toast) App.toast(r.message, r.accounts ? "info" : "warning");
      if (!r.accounts) {
        setBusy(false);
        return;
      }
      poll(1200);
    } catch (e) {
      setBusy(false);
      if (App.toast) App.toast(e.message, "error");
    }
  }

  function initRefresh() {
    doc.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-an-refresh]");
      if (!btn) return;
      e.preventDefault();
      refresh();
    });
    const r = root();
    if (r && r.querySelector(".an-refresh__btn.is-busy")) {
      setBusy(true);
      poll(1500);
    }
  }

  // Period / tab links: keep the frame while the next page loads (no skeleton flash).
  function initLoading() {
    doc.addEventListener("click", (e) => {
      const a = e.target.closest(".an-dd__item[href], .an-tabs a[href], .an-side__row[href]");
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      const main = doc.getElementById("an-main");
      if (main) main.classList.add("is-loading");
    });
    window.addEventListener("pageshow", () => {
      const main = doc.getElementById("an-main");
      if (main) main.classList.remove("is-loading");
    });
  }

  function init() {
    const el = doc.getElementById("an-data");
    if (el) {
      try {
        data = JSON.parse(el.textContent || "{}") || { charts: {} };
      } catch (e) {
        data = { charts: {} };
      }
    }
    mountAll();
    initHeatmap();
    initSort();
    initRefresh();
    initLoading();
    window.addEventListener("themechange", () => requestAnimationFrame(mountAll));
    window.addEventListener("scroll", hideTip, { passive: true });
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();
