/* NAS dashboard front-end. No build step: plain JS + Chart.js. */
(() => {
  "use strict";

  // ------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt1 = (v) => (v == null ? "–" : (Math.round(v * 10) / 10).toFixed(1));
  const fmt0 = (v) => (v == null ? "–" : String(Math.round(v)));

  function bytes(n, dec = 1) {
    if (n == null) return "–";
    const u = ["B", "KB", "MB", "GB", "TB", "PB"];
    let i = 0;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return `${n.toFixed(i === 0 ? 0 : dec)} ${u[i]}`;
  }
  function duration(s) {
    s = Math.floor(s);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }
  function hhmm(ms) {
    const d = new Date(ms);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-NasDash": "1", ...(opts.headers || {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401 && path !== "/api/login") {
      location.href = "/login";
      throw new Error("Logged out");
    }
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  let toastTimer;
  function toast(msg, err = false) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast show" + (err ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.className = "toast"), 3200);
  }

  function confirmDialog(title, text, okLabel) {
    return new Promise((resolve) => {
      const dlg = $("confirmDlg");
      $("confirmTitle").textContent = title;
      $("confirmText").textContent = text;
      $("confirmOk").textContent = okLabel;
      dlg.returnValue = "";
      dlg.onclose = () => resolve(dlg.returnValue === "ok");
      dlg.showModal();
    });
  }

  // ------------------------------------------------------------ state
  let status = null;
  let cfg = null;
  let editKey = null;
  let draft = null;
  let dirty = false;
  let historyMinutes = 60;

  // Colours follow the drive, not its position: first drive seen gets slot 1, etc.
  const SLOTS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s7", "--s8"];
  const driveSlot = {};
  function driveColor(name) {
    if (!(name in driveSlot)) driveSlot[name] = Object.keys(driveSlot).length % SLOTS.length;
    return css(SLOTS[driveSlot[name]]);
  }

  // ------------------------------------------------------------ chart defaults
  function chartTheme() {
    Chart.defaults.color = css("--muted");
    Chart.defaults.borderColor = css("--grid");
    Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
    Chart.defaults.font.size = 11.5;
  }
  function tooltipStyle() {
    return {
      backgroundColor: css("--surface"),
      titleColor: css("--ink"),
      bodyColor: css("--ink-2"),
      borderColor: css("--border"),
      borderWidth: 1,
      padding: 10,
      boxPadding: 4,
      usePointStyle: true,
    };
  }

  // Vertical dashed line at the current control temperature.
  const nowLine = {
    id: "nowLine",
    afterDatasetsDraw(chart) {
      const t = status?.status?.temp;
      if (t == null) return;
      const x = chart.scales.x.getPixelForValue(t);
      const { top, bottom, left, right } = chart.chartArea;
      if (x < left || x > right) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = css("--ink-2");
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = css("--ink-2");
      ctx.font = "11px system-ui, sans-serif";
      ctx.textAlign = x > right - 60 ? "right" : "left";
      ctx.fillText(`now ${fmt1(t)} °C`, x + (ctx.textAlign === "left" ? 5 : -5), top + 11);
      ctx.restore();
    },
  };

  // ------------------------------------------------------------ curve editor chart
  let curveChart;
  function buildCurveChart() {
    const accent = css("--accent");
    curveChart = new Chart($("curveChart"), {
      type: "line",
      data: {
        datasets: [
          {
            label: "Curve",
            data: [],
            borderColor: accent,
            backgroundColor: accent,
            borderWidth: 2,
            pointRadius: 6,
            pointHoverRadius: 8,
            pointBorderColor: css("--surface"),
            pointBorderWidth: 2,
            tension: 0,
          },
          {
            label: "Now",
            data: [],
            showLine: false,
            pointRadius: 5,
            pointStyle: "rectRot",
            backgroundColor: css("--ink"),
            borderColor: css("--surface"),
            borderWidth: 2,
          },
        ],
      },
      options: {
        animation: false,
        maintainAspectRatio: false,
        parsing: false,
        clip: false,
        layout: { padding: { top: 10, right: 10 } },
        scales: {
          x: { type: "linear", min: 20, max: 60, title: { display: true, text: "Temperature (°C)" }, ticks: { stepSize: 5 } },
          y: { min: 0, max: 100, title: { display: true, text: "Fan (%)" }, ticks: { stepSize: 20 } },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            ...tooltipStyle(),
            filter: (item) => item.datasetIndex === 0 || item.datasetIndex === 1,
            callbacks: {
              title: () => "",
              label: (c) => `${c.datasetIndex === 1 ? "Now: " : ""}${fmt1(c.parsed.x)} °C → ${fmt0(c.parsed.y)} %`,
            },
          },
        },
      },
      plugins: [nowLine],
    });
    attachDrag(curveChart);
  }

  function attachDrag(chart) {
    const canvas = chart.canvas;
    let dragIdx = null;

    const valueAt = (ev) => {
      const r = canvas.getBoundingClientRect();
      const px = ev.clientX - r.left, py = ev.clientY - r.top;
      return { px, py, x: chart.scales.x.getValueForPixel(px), y: chart.scales.y.getValueForPixel(py) };
    };
    const nearest = (px, py) => {
      const meta = chart.getDatasetMeta(0);
      let best = null, bestD = 14;
      meta.data.forEach((pt, i) => {
        const d = Math.hypot(pt.x - px, pt.y - py);
        if (d < bestD) { bestD = d; best = i; }
      });
      return best;
    };

    canvas.addEventListener("pointerdown", (ev) => {
      if (!draft || draft.mode !== "curve" || ev.button !== 0) return;
      const { px, py } = valueAt(ev);
      dragIdx = nearest(px, py);
      if (dragIdx != null) { canvas.setPointerCapture(ev.pointerId); canvas.style.cursor = "grabbing"; }
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (!draft || draft.mode !== "curve") return;
      const { px, py, x, y } = valueAt(ev);
      if (dragIdx == null) {
        canvas.style.cursor = nearest(px, py) != null ? "grab" : "crosshair";
        return;
      }
      const pts = draft.points;
      const lo = dragIdx > 0 ? pts[dragIdx - 1][0] + 0.5 : chart.scales.x.min;
      const hi = dragIdx < pts.length - 1 ? pts[dragIdx + 1][0] - 0.5 : chart.scales.x.max;
      // Keep the curve rising: a point can't go below its left or above its right neighbour.
      const yLo = dragIdx > 0 ? pts[dragIdx - 1][1] : 0;
      const yHi = dragIdx < pts.length - 1 ? pts[dragIdx + 1][1] : 100;
      pts[dragIdx] = [
        Math.round(Math.min(hi, Math.max(lo, x)) * 2) / 2,
        Math.round(Math.min(yHi, Math.max(yLo, y))),
      ];
      markDirty();
      renderCurve();
    });
    const end = () => { dragIdx = null; canvas.style.cursor = ""; };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);

    canvas.addEventListener("dblclick", (ev) => {
      if (!draft || draft.mode !== "curve") return;
      const { px, py, x, y } = valueAt(ev);
      if (nearest(px, py) != null) return;
      const t = Math.round(x * 2) / 2;
      if (draft.points.some((p) => Math.abs(p[0] - t) < 0.5)) return;
      draft.points.push([t, Math.round(Math.min(100, Math.max(0, y)))]);
      draft.points.sort((a, b) => a[0] - b[0]);
      // Snap the new point between its neighbours so the curve keeps rising.
      const i = draft.points.findIndex((p) => p[0] === t);
      const yLo = i > 0 ? draft.points[i - 1][1] : 0, yHi = i < draft.points.length - 1 ? draft.points[i + 1][1] : 100;
      draft.points[i][1] = Math.min(yHi, Math.max(yLo, draft.points[i][1]));
      markDirty();
      renderCurve();
    });
    canvas.addEventListener("contextmenu", (ev) => {
      if (!draft || draft.mode !== "curve") return;
      const { px, py } = valueAt(ev);
      const i = nearest(px, py);
      if (i == null) return;
      ev.preventDefault();
      if (draft.points.length <= 2) { toast("A curve needs at least 2 points", true); return; }
      draft.points.splice(i, 1);
      markDirty();
      renderCurve();
    });
  }

  function interpolate(points, t) {
    const p = [...points].sort((a, b) => a[0] - b[0]);
    if (t <= p[0][0]) return p[0][1];
    if (t >= p[p.length - 1][0]) return p[p.length - 1][1];
    for (let i = 0; i < p.length - 1; i++) {
      const [t0, s0] = p[i], [t1, s1] = p[i + 1];
      if (t >= t0 && t <= t1) return t1 === t0 ? s1 : s0 + ((s1 - s0) * (t - t0)) / (t1 - t0);
    }
    return p[p.length - 1][1];
  }

  function renderCurve() {
    if (!curveChart || !draft || draft.mode !== "curve") return;
    const pts = draft.points;
    const xs = pts.map((p) => p[0]);
    curveChart.options.scales.x.min = Math.min(20, Math.floor((Math.min(...xs) - 2) / 5) * 5);
    curveChart.options.scales.x.max = Math.max(60, Math.ceil((Math.max(...xs) + 2) / 5) * 5);
    curveChart.data.datasets[0].data = pts.map(([x, y]) => ({ x, y }));
    const t = status?.status?.temp;
    const liveProfile = status?.status?.profile === editKey;
    curveChart.data.datasets[1].data =
      t != null ? [{ x: t, y: liveProfile && status?.status?.target != null ? status.status.target : interpolate(pts, t) }] : [];
    curveChart.update("none");
    $("pointsTable").innerHTML = pts.map(([x, y]) => `<span>${x} °C → ${y} %</span>`).join("");
  }

  // ------------------------------------------------------------ history charts
  // One chart per measure, each with a single y axis. Values are read by hovering.
  const charts = {};
  function timeChart(id, { yMin, yMax, yTick, tip, fill = false }) {
    const accent = css("--accent");
    return new Chart($(id), {
      type: "line",
      data: { datasets: fill ? [{ label: "", data: [], borderColor: accent, backgroundColor: accent + "22", fill: true }] : [] },
      options: {
        animation: false,
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        interaction: { mode: "index", intersect: false },
        elements: { point: { radius: 0, hoverRadius: 4 }, line: { borderWidth: 2, tension: 0.2 } },
        scales: {
          x: { type: "linear", ticks: { callback: (v) => hhmm(v), maxTicksLimit: 6, maxRotation: 0 }, grid: { display: false } },
          y: { min: yMin, max: yMax, suggestedMin: 0, ticks: { maxTicksLimit: 5, callback: yTick } },
        },
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipStyle(), callbacks: { title: (i) => hhmm(i[0].parsed.x), label: (c) => ` ${tip(c.parsed.y, c.dataset.label)}` } },
        },
      },
    });
  }

  function buildHistoryCharts() {
    const pct = (v) => `${v}%`;
    charts.temp = timeChart("tempChart", { yTick: (v) => `${v}°`, tip: (v, l) => `${l}: ${fmt1(v)} °C` });
    charts.temp.options.scales.y.suggestedMin = undefined;
    charts.fan = timeChart("fanChart", { yMin: 0, yMax: 100, yTick: pct, tip: (v) => `Fan: ${fmt0(v)} %`, fill: true });
    charts.rpm = timeChart("rpmChart", { yTick: (v) => v.toLocaleString(), tip: (v) => `${fmt0(v)} RPM`, fill: true });
    charts.load = timeChart("loadChart", { yMin: 0, yMax: 100, yTick: pct, tip: (v) => `CPU: ${fmt0(v)} %`, fill: true });
    charts.mem = timeChart("memChart", { yMin: 0, yMax: 100, yTick: pct, tip: (v) => `Memory: ${fmt0(v)} %`, fill: true });
    charts.net = timeChart("netChart", { yTick: (v) => bytes(v, 0), tip: (v, l) => `${l}: ${bytes(v)}/s` });
    charts.io = timeChart("ioChart", { yTick: (v) => bytes(v, 0), tip: (v, l) => `${l}: ${bytes(v)}/s` });
    charts.io.options.elements.line.tension = 0;
  }

  function legendHtml(datasets) {
    return datasets
      .map((d) => {
        const bg = d.borderDash
          ? `repeating-linear-gradient(90deg,${d.borderColor} 0 5px,transparent 5px 8px)`
          : d.borderColor;
        return `<span><i style="background:${bg}"></i>${esc(d.label)}</span>`;
      })
      .join("");
  }

  async function loadHistory() {
    let rows;
    try { rows = await api(`/api/history?minutes=${historyMinutes}`); } catch (_) { return; }
    const pts = (key) => rows.map((r) => ({ x: r.t * 1000, y: r[key] ?? null }));

    // temperatures: one line per drive (colour follows the drive) + CPU dashed
    const names = [];
    rows.forEach((r) => Object.keys(r.drives || {}).forEach((n) => { if (!names.includes(n)) names.push(n); }));
    names.sort();
    const temps = names.map((n) => {
      const color = driveColor(n);
      return { label: n, data: rows.map((r) => ({ x: r.t * 1000, y: r.drives?.[n] ?? null })), borderColor: color, backgroundColor: color };
    });
    const ink2 = css("--ink-2");
    temps.push({ label: "CPU", data: pts("cpu"), borderColor: ink2, backgroundColor: ink2, borderDash: [5, 4], borderWidth: 1.5 });
    charts.temp.data.datasets = temps;
    $("tempLegend").innerHTML = legendHtml(temps);

    // Disk activity: one colour per drive (same as temperatures), reads solid, writes dashed.
    const ioNames = [];
    rows.forEach((r) => Object.keys(r.io || {}).forEach((n) => { if (!ioNames.includes(n)) ioNames.push(n); }));
    ioNames.sort();
    const ioSets = [];
    ioNames.forEach((n) => {
      const color = driveColor(n);
      const val = (r, i) => (r.io && r.io[n] ? r.io[n][i] : null);
      ioSets.push({ label: `${n} read`, data: rows.map((r) => ({ x: r.t * 1000, y: val(r, 0) })), borderColor: color, backgroundColor: color });
      ioSets.push({ label: `${n} write`, data: rows.map((r) => ({ x: r.t * 1000, y: val(r, 1) })), borderColor: color, backgroundColor: color, borderDash: [5, 4] });
    });
    charts.io.data.datasets = ioSets;
    const swatch = (bg) => `<i style="background:${bg}"></i>`;
    const ink2io = css("--ink-2");
    $("ioLegend").innerHTML =
      ioNames.map((n) => `<span>${swatch(driveColor(n))}${esc(n)}</span>`).join("") +
      `<span class="legend-key">${swatch(ink2io)}read</span>` +
      `<span>${swatch(`repeating-linear-gradient(90deg,${ink2io} 0 5px,transparent 5px 8px)`)}write</span>`;

    charts.fan.data.datasets[0].data = pts("fan");
    charts.rpm.data.datasets[0].data = pts("rpm");
    charts.load.data.datasets[0].data = pts("load");
    charts.mem.data.datasets[0].data = pts("mem");

    const down = css("--s1"), up = css("--s2");
    charts.net.data.datasets = [
      { label: "Down", data: pts("rx"), borderColor: down, backgroundColor: down },
      { label: "Up", data: pts("tx"), borderColor: up, backgroundColor: up },
    ];
    $("netLegend").innerHTML = legendHtml(charts.net.data.datasets);

    // Fixed window: every x axis spans the selected range and ends now.
    const now = Date.now();
    Object.values(charts).forEach((c) => {
      c.options.scales.x.min = now - historyMinutes * 60000;
      c.options.scales.x.max = now;
      // Ticks on round times: every 3 min / 10 min / 1 h.
      c.options.scales.x.ticks.stepSize = (historyMinutes <= 15 ? 3 : historyMinutes <= 60 ? 10 : 60) * 60000;
      c.options.scales.x.ticks.includeBounds = false;
      c.update("none");
    });
    $("historyHint").textContent = rows.length < 3
      ? "Collecting data… the charts fill in over the next minutes."
      : "Hover a chart to see exact values. History is kept in memory and starts over when the service restarts.";
  }

  // ------------------------------------------------------------ profiles
  function profileSummary(p) {
    if (p.mode === "fixed") return `Fixed ${p.percent}%`;
    if (p.mode === "pid") return `PID · target ${p.target} °C`;
    const pts = p.points;
    return `Curve · ${pts[0][1]}–${pts[pts.length - 1][1]}%`;
  }

  function renderProfiles() {
    const list = $("profileList");
    list.innerHTML = "";
    Object.entries(cfg.profiles).forEach(([key, p]) => {
      const b = document.createElement("button");
      b.className = "profile";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(key === cfg.active_profile));
      b.innerHTML = `${esc(p.name)}<small>${esc(profileSummary(p))}</small>`;
      b.onclick = () => activate(key);
      list.appendChild(b);
    });
    const sel = $("editSelect");
    sel.innerHTML = Object.entries(cfg.profiles)
      .map(([k, p]) => `<option value="${esc(k)}">${esc(p.name)}${k === cfg.active_profile ? " (active)" : ""}</option>`)
      .join("");
    sel.value = editKey;
  }

  function loadDraft(key) {
    editKey = key;
    draft = JSON.parse(JSON.stringify(cfg.profiles[key]));
    dirty = false;
    fillEditor();
  }

  function defaultsFor(mode, from) {
    const name = from.name;
    if (mode === "curve") return { name, mode, points: [[30, 25], [35, 35], [40, 55], [45, 80], [48, 100]], hysteresis: 2 };
    if (mode === "pid") return { name, mode, target: 38, kp: 8, ki: 0.15, kd: 0, min: 20, max: 100 };
    return { name, mode, percent: from.mode === "pid" ? from.max : 60 };
  }

  function fillEditor() {
    $("editSelect").value = editKey;
    $("editMode").value = draft.mode;
    $("editName").value = draft.name;
    $("curveBox").hidden = draft.mode !== "curve";
    $("pidBox").hidden = draft.mode !== "pid";
    $("fixedBox").hidden = draft.mode !== "fixed";
    if (draft.mode === "curve") {
      $("editHyst").value = draft.hysteresis;
      renderCurve();
    } else if (draft.mode === "pid") {
      $("pidTarget").value = draft.target; $("pidMin").value = draft.min; $("pidMax").value = draft.max;
      $("pidKp").value = draft.kp; $("pidKi").value = draft.ki; $("pidKd").value = draft.kd;
    } else {
      $("fixedPct").value = draft.percent; $("fixedOut").textContent = `${draft.percent}%`;
    }
    const builtin = cfg.builtin.includes(editKey);
    $("deleteProfile").disabled = builtin;
    $("deleteProfile").title = builtin ? "Built-in profiles can't be deleted" : "";
    $("activateProfile").disabled = editKey === cfg.active_profile && !dirty;
    updateSaveState();
  }

  function markDirty() { dirty = true; updateSaveState(); }
  function updateSaveState() {
    $("saveProfile").textContent = dirty ? "Save changes" : "Saved";
    $("saveProfile").disabled = !dirty;
    $("activateProfile").disabled = editKey === cfg.active_profile && !dirty;
  }

  function readEditor() {
    draft.name = $("editName").value.trim() || "Custom";
    if (draft.mode === "curve") draft.hysteresis = parseFloat($("editHyst").value) || 0;
    if (draft.mode === "pid") {
      draft.target = parseFloat($("pidTarget").value); draft.min = parseInt($("pidMin").value);
      draft.max = parseInt($("pidMax").value); draft.kp = parseFloat($("pidKp").value);
      draft.ki = parseFloat($("pidKi").value); draft.kd = parseFloat($("pidKd").value);
    }
    if (draft.mode === "fixed") draft.percent = parseInt($("fixedPct").value);
    return draft;
  }

  async function saveProfile(key) {
    readEditor();
    try {
      await api(`/api/profiles/${key}`, { method: "PUT", body: draft });
      await loadConfig(key);
      toast("Profile saved");
      return true;
    } catch (e) { toast(e.message, true); return false; }
  }

  async function activate(key) {
    if (dirty && key === editKey && !(await saveProfile(key))) return;
    try {
      await api("/api/profile/active", { method: "POST", body: { key } });
      cfg.active_profile = key;
      if (!dirty || key !== editKey) loadDraft(key);
      renderProfiles();
      fillEditor();
      toast(`Using “${cfg.profiles[key].name}”`);
      refresh();
    } catch (e) { toast(e.message, true); }
  }

  async function loadConfig(keepKey) {
    cfg = await api("/api/config");
    renderProfiles();
    loadDraft(keepKey && cfg.profiles[keepKey] ? keepKey : cfg.active_profile);
    const f = $("ctlForm");
    Object.entries(cfg.control).forEach(([k, v]) => { if (f.elements[k]) f.elements[k].value = v; });
  }

  function wireEditor() {
    $("editSelect").onchange = async (e) => {
      if (dirty && !(await confirmDialog("Discard changes?", "You have unsaved changes to this profile.", "Discard"))) {
        e.target.value = editKey; return;
      }
      loadDraft(e.target.value);
    };
    $("editMode").onchange = (e) => { draft = defaultsFor(e.target.value, draft); markDirty(); fillEditor(); };
    ["editName", "editHyst", "pidTarget", "pidMin", "pidMax", "pidKp", "pidKi", "pidKd"].forEach((id) =>
      $(id).addEventListener("input", markDirty));
    $("fixedPct").oninput = (e) => { $("fixedOut").textContent = `${e.target.value}%`; markDirty(); };
    $("saveProfile").onclick = () => saveProfile(editKey);
    $("activateProfile").onclick = () => activate(editKey);
    $("saveAsProfile").onclick = async () => {
      const name = prompt("Name for the new profile:", `${$("editName").value} (copy)`);
      if (!name) return;
      let base = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 24) || "custom";
      let key = base, n = 2;
      while (cfg.profiles[key]) key = `${base}_${n++}`;
      $("editName").value = name;
      if (await saveProfile(key)) { loadDraft(key); renderProfiles(); }
    };
    $("deleteProfile").onclick = async () => {
      if (!(await confirmDialog("Delete profile?", `“${draft.name}” will be removed.`, "Delete"))) return;
      try {
        await api(`/api/profiles/${editKey}`, { method: "DELETE" });
        await loadConfig();
        toast("Profile deleted");
      } catch (e) { toast(e.message, true); }
    };

    // override
    $("ovPct").oninput = (e) => ($("ovOut").textContent = `${e.target.value}%`);
    $("ovApply").onclick = async () => {
      try {
        await api("/api/fan/override", { method: "POST", body: { percent: +$("ovPct").value, minutes: +$("ovMin").value } });
        toast("Override active"); refresh();
      } catch (e) { toast(e.message, true); }
    };
    $("ovClear").onclick = async () => {
      try { await api("/api/fan/override", { method: "POST", body: { percent: null } }); toast("Override cleared"); refresh(); }
      catch (e) { toast(e.message, true); }
    };

    // controller settings
    $("ctlForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const body = Object.fromEntries(new FormData(ev.target).entries());
      try {
        const r = await api("/api/control", { method: "PUT", body });
        Object.entries(r.control).forEach(([k, v]) => { if (ev.target.elements[k]) ev.target.elements[k].value = v; });
        toast("Settings saved");
      } catch (e) { toast(e.message, true); }
    };

    // history range
    $("rangeSeg").querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        $("rangeSeg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
        historyMinutes = +b.dataset.min;
        loadHistory();
      };
    });

    $("drivesRefresh").onclick = async () => {
      try { await api("/api/drives/refresh", { method: "POST" }); toast("Reading drives…"); setTimeout(refresh, 4000); }
      catch (e) { toast(e.message, true); }
    };

    // power
    document.querySelectorAll("[data-power]").forEach((b) => {
      b.onclick = async () => {
        const action = b.dataset.power;
        const ok = await confirmDialog(
          action === "reboot" ? "Reboot the NAS?" : "Shut down the NAS?",
          action === "reboot"
            ? "The dashboard will be unreachable for about a minute."
            : "You'll need physical access to turn it back on.",
          action === "reboot" ? "Reboot" : "Shut down",
        );
        if (!ok) return;
        try {
          const r = await api(`/api/power/${action}`, { method: "POST" });
          toast(r.mock ? `Mock mode: would ${action} now` : action === "reboot" ? "Rebooting…" : "Shutting down…");
        } catch (e) { toast(e.message, true); }
      };
    });
  }

  // ------------------------------------------------------------ status rendering
  function tempClass(t, warn, crit) { return t == null ? "" : t >= crit ? "critical" : t >= warn ? "warning" : "good"; }

  function renderStatus() {
    const s = status, st = s.status, sys = s.system || {}, fan = s.fan || {};
    document.title = `${sys.hostname || "NAS"} · ${location.hash === "#settings" ? "Settings" : "NAS Dashboard"}`;
    $("logoutBtn").hidden = !s.auth_enabled;
    $("hostname").textContent = sys.hostname || "NAS";
    $("subline").textContent = [sys.model, sys.uptime_s != null ? `up ${duration(sys.uptime_s)}` : null].filter(Boolean).join(" · ");
    $("authBanner").hidden = s.auth_enabled;
    $("mockBanner").hidden = !s.mock;
    $("version").textContent = `nasdash ${s.version} · written by Claude, an AI model by Anthropic`;

    const stalled = fan.rpm === 0 && st.target >= 30;

    // fan icon spin speed follows RPM
    const icon = $("fanIcon");
    if (fan.rpm > 0) {
      icon.classList.add("spin");
      icon.style.setProperty("--spin", `${Math.max(0.25, Math.min(3, 1200 / fan.rpm)).toFixed(2)}s`);
    } else icon.classList.remove("spin");

    // reason chip
    const r = $("reason");
    const prof = cfg?.profiles?.[st.profile];
    r.textContent = `${prof ? prof.name : st.profile} · ${st.reason}`;
    r.className = "reason" + (/error|fail-safe/.test(st.reason || "") ? " bad" : "");

    // alerts
    const alerts = [];
    if (/error|fail-safe/.test(st.reason || "")) alerts.push(`Fan control problem: ${st.reason}`);
    if (fan.error) alerts.push(`Fan controller (EMC2101) could not be opened: ${fan.error}`);
    if (stalled) alerts.push("The fan reports 0 RPM although it should be spinning. Check the fan cable and the TACH connection.");
    (s.drives || []).forEach((d) => {
      if (d.health === false) alerts.push(`${d.name}: SMART health check FAILED. Back up your data.`);
      if (d.state === "error") alerts.push(`${d.name}: could not read SMART data (${d.error || "unknown error"}).`);
    });
    $("alertBanner").hidden = !alerts.length;
    $("alertBanner").innerHTML = alerts.map(esc).join("<br>");

    // override
    const ov = st.override;
    $("ovState").textContent = ov ? `Active: ${ov.percent}% until ${hhmm(ov.until * 1000)}` : "Not active";

    renderDrives();
    renderSystem();
    if (draft?.mode === "curve") renderCurve();
  }

  function renderDrives() {
    const drives = status.drives || [];
    $("drivesPolled").textContent = status.drives_polled ? `· read ${hhmm(status.drives_polled * 1000)}` : "";
    $("spindownInfo").innerHTML = status.spindown_min > 0
      ? `Auto spin-down after ${status.spindown_min} min`
      : `Auto spin-down off <a href="#settings">(change)</a>`;
    if (!drives.length) {
      $("driveRows").innerHTML = `<tr><td colspan="10" class="muted">${status.drives_polled ? "No drives found." : "Waiting for first reading…"}</td></tr>`;
      return;
    }
    $("driveRows").innerHTML = drives
      .map((d) => {
        const state =
          d.state === "active" ? `<span class="status good">Active</span>`
          : d.state === "standby" ? `<span class="status idle">Spun down</span>`
          : `<span class="status critical" title="${esc(d.error)}">Error</span>`;
        const health =
          d.health === true ? `<span class="status good">Passed</span>`
          : d.health === false ? `<span class="status critical">FAILED</span>`
          : `<span class="muted">–</span>`;
        const tcls = tempClass(d.temp, 45, 50);
        const temp = d.temp != null ? `<span class="status ${tcls}">${fmt0(d.temp)} °C</span>` : `<span class="muted">–</span>`;
        const poh = d.power_on_hours != null ? `${Math.round(d.power_on_hours / 24).toLocaleString()} d` : "–";
        // How long since anything read from or wrote to the drive.
        let activity = `<span class="muted">–</span>`;
        if (d.idle_s != null) {
          const t = d.idle_s < 60 ? "just now" : `${duration(d.idle_s)} ago`;
          activity = d.idle_s < 60 && d.state === "active" ? `<span class="status warning">In use</span>` : t;
        }
        if (d.spindowns_24h) activity += `<div class="muted" style="font-size:11.5px">spun down ${d.spindowns_24h}× in 24 h</div>`;
        if (d.spindown_error) activity += `<div class="status critical" style="font-size:11.5px" title="${esc(d.spindown_error)}">spin-down failed</div>`;
        const sleepBtn = d.rotational && d.state !== "standby"
          ? `<button class="btn ghost small" data-sleep="${esc(d.name)}" title="Spin this drive down now">Sleep</button>` : "";
        const bad = (d.reallocated ?? 0) + (d.pending ?? 0);
        const rp = d.reallocated == null && d.pending == null ? "–"
          : `<span class="${bad ? "status serious" : ""}">${d.reallocated ?? "–"} / ${d.pending ?? "–"}</span>`;
        return `<tr>
          <td class="dev"><span class="swatch" style="background:${driveColor(d.name)}"></span>${esc(d.name)}</td>
          <td>${esc(d.model || "–")}<div class="muted" style="font-size:11.5px">${esc(d.serial || "")}${d.rotational === false ? " · SSD" : ""}</div></td>
          <td class="num">${bytes(d.size, 1)}</td>
          <td>${state}</td>
          <td>${activity}</td>
          <td class="num">${temp}</td>
          <td>${health}</td>
          <td class="num">${poh}</td>
          <td class="num">${rp}</td>
          <td class="num">${sleepBtn}</td>
        </tr>`;
      })
      .join("");
    $("driveRows").querySelectorAll("[data-sleep]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api(`/api/drives/${b.dataset.sleep}/standby`, { method: "POST" });
          toast(`${b.dataset.sleep} spun down. If it wakes up again right away, something is using it.`);
          refresh();
        } catch (e) { toast(e.message, true); b.disabled = false; }
      };
    });
  }

  function bar(name, val, pct, cls = "") {
    return `<div class="bar"><span class="name" title="${esc(name)}">${esc(name)}</span><span class="val">${val}</span>
      <div class="track"><div class="fill ${cls}" style="width:${Math.max(0, Math.min(100, pct))}%"></div></div></div>`;
  }

  function renderSystem() {
    const s = status.system || {};
    const rows = [
      ["Model", s.model], ["OS", s.os], ["Kernel", s.kernel], ["Hostname", s.hostname],
      ["IP", (s.ips || []).join("<br>") || "–"],
      ["Uptime", s.uptime_s != null ? duration(s.uptime_s) : "–"],
      ["Power", s.throttled ? (s.throttled.flags.length ? s.throttled.flags.join("<br>") : "OK, no throttling or under-voltage") : "–"],
    ];
    $("sysInfo").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${k === "IP" || k === "Power" ? v : esc(v ?? "–")}</dd>`).join("");
    $("mountBars").innerHTML = (s.mounts || [])
      .map((m) => bar(`${m.mount} (${m.device.replace("/dev/", "")})`, `${bytes(m.used)} / ${bytes(m.total)}`, m.percent,
        m.percent > 95 ? "critical" : m.percent > 85 ? "warning" : ""))
      .join("") || `<span class="muted">No mounted drives</span>`;
  }

  // ------------------------------------------------------------ pages
  // Two views in one page: the dashboard (#) and settings (#settings).
  function route() {
    const settings = location.hash === "#settings";
    $("viewDashboard").hidden = settings;
    $("viewSettings").hidden = !settings;
    const nav = $("navBtn");
    nav.textContent = settings ? "Dashboard" : "Settings";
    nav.href = settings ? "#" : "#settings";
    document.title = `${status?.system?.hostname || "NAS"} · ${settings ? "Settings" : "NAS Dashboard"}`;
    window.scrollTo(0, 0);
    // Charts drawn while hidden need a resize once they're visible.
    requestAnimationFrame(() => {
      if (settings) { curveChart?.resize(); renderCurve(); }
      else Object.values(charts).forEach((c) => c.resize());
    });
  }

  // ------------------------------------------------------------ polling
  let failures = 0;
  async function refresh() {
    try {
      status = await api("/api/status");
      failures = 0;
      $("connChip").className = "chip ok";
      $("connChip").textContent = "Live";
      renderStatus();
    } catch (e) {
      failures++;
      if (failures > 1) {
        $("connChip").className = "chip bad";
        $("connChip").textContent = "Offline";
      }
    }
  }

  async function init() {
    chartTheme();
    buildCurveChart();
    buildHistoryCharts();
    wireEditor();
    window.addEventListener("hashchange", route);
    route();
    $("logoutBtn").onclick = async () => {
      try { await api("/api/logout", { method: "POST" }); } catch (_) {}
      location.href = "/login";
    };
    try { await loadConfig(); } catch (e) { toast(`Could not load config: ${e.message}`, true); }
    await refresh();
    await loadHistory();
    setInterval(refresh, 3000);
    setInterval(loadHistory, 5000);
    // Re-theme charts if the OS switches between light and dark.
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());
    window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  }
  init();
})();
