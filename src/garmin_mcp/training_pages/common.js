/* Shared helpers for the training dashboards. Exposes window.T. */
(function () {
  const DAY = 86400000;
  const T = {};

  T.qs = new URLSearchParams(location.search);
  T.key = T.qs.get("key") || "";

  T.load = async function (refresh) {
    const url = "/training/data.json?key=" + encodeURIComponent(T.key) + (refresh ? "&refresh=1" : "");
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    return T.prepare(await res.json());
  };

  // Run `render(data)` once the data has loaded; shows loading and error states.
  T.boot = function (render) {
    const note = document.createElement("div");
    note.style.cssText = "position:fixed;inset:0;display:flex;align-items:center;justify-content:center;font:15px system-ui;color:#888;background:inherit;z-index:99";
    note.textContent = "Loading Garmin data…";
    document.body.appendChild(note);
    T.load(T.qs.get("refresh") === "1")
      .then((d) => { note.remove(); render(d); })
      .catch((e) => { note.textContent = "Couldn't load Garmin data: " + e.message; });
  };

  T.refreshButton = function (el) {
    el.style.cursor = "pointer";
    el.title = "Pull fresh data from Garmin";
    el.onclick = async () => {
      el.textContent = "refreshing…";
      try { await T.load(true); location.reload(); } catch (e) { el.textContent = "refresh failed"; }
    };
  };

  // ---------- formatting ----------
  T.d = (s) => new Date((s || "").slice(0, 10) + "T12:00:00");
  T.iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  T.today = () => T.d(T.iso(new Date()));
  T.km = (m) => (m || 0) / 1000;
  T.fmtKm = (m, dp = 1) => T.km(m).toFixed(dp);
  T.pace = (mps) => {
    if (!mps) return "–";
    const s = 1000 / mps, m = Math.floor(s / 60), r = Math.round(s % 60);
    return (r === 60 ? m + 1 + ":00" : m + ":" + String(r).padStart(2, "0"));
  };
  T.dur = (s) => {
    if (s == null) return "–";
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? h + "h " + String(m).padStart(2, "0") + "m" : m + " min";
  };
  T.hm = (s) => (s == null ? "–" : (s / 3600).toFixed(1) + " h");
  T.dateLabel = (s, opts) => T.d(s).toLocaleDateString("en-GB", opts || { day: "numeric", month: "short" });
  T.round = (v, dp = 0) => (v == null || isNaN(v) ? null : Number(v.toFixed(dp)));
  T.mean = (a) => { const v = a.filter((x) => x != null && !isNaN(x)); return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null; };
  T.sum = (a) => a.reduce((s, x) => s + (x || 0), 0);
  T.monday = (d) => { const x = new Date(d); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return T.d(T.iso(x)); };
  T.daysBetween = (a, b) => Math.round((T.d(T.iso(b)) - T.d(T.iso(a))) / DAY);

  // ---------- derived data ----------
  T.prepare = function (doc) {
    const acts = (doc.activities || []).filter((a) => a.start).map((a) => ({ ...a, date: a.start.slice(0, 10) }));
    acts.sort((a, b) => (a.start < b.start ? 1 : -1));
    doc.acts = acts;
    doc.runs = acts.filter((a) => a.sport === "run" && a.distance_m > 0);
    const by = (rows, f) => Object.fromEntries((rows || []).filter((r) => r && r.date).map((r) => [r.date, f(r)]));
    doc.sleepBy = by(doc.sleep, (r) => r);
    doc.hrvBy = by(doc.hrv, (r) => r);
    doc.rhrBy = by(doc.resting_hr, (r) => r.rhr);
    doc.stressBy = by(doc.stress, (r) => r.stress);
    doc.bbBy = by(doc.body_battery, (r) => r);
    doc.cfg = doc.config || {};
    doc.vitals = T.vitals(doc);
    doc.form = T.formSeries(doc, 120);
    return doc;
  };

  // Last n days (oldest first) as ISO dates, ending today.
  T.days = (n) => { const out = [], t = T.today(); for (let i = n - 1; i >= 0; i--) out.push(T.iso(new Date(t - i * DAY))); return out; };
  T.series = (by, n, f) => T.days(n).map((d) => (by[d] == null ? null : f ? f(by[d]) : by[d]));

  // Fitness (42-day average load), fatigue (7-day), form = fitness - fatigue.
  T.formSeries = function (doc, n) {
    const load = {};
    doc.acts.forEach((a) => { load[a.date] = (load[a.date] || 0) + (a.load || 0); });
    const all = T.days(n + 42), out = [];
    for (let i = 42; i < all.length; i++) {
      const fit = T.sum(all.slice(i - 41, i + 1).map((d) => load[d] || 0)) / 42;
      const fat = T.sum(all.slice(i - 6, i + 1).map((d) => load[d] || 0)) / 7;
      out.push({ date: all[i], fitness: fit, fatigue: fat, form: fit - fat, load: load[all[i]] || 0 });
    }
    return out;
  };

  T.vitals = function (doc) {
    const t = doc.today || {}, s = t.summary || {}, sl = t.sleep || {}, h = t.hrv || {};
    const last = (by) => { const k = Object.keys(by).sort().pop(); return k ? by[k] : null; };
    const hrvLast = last(doc.hrvBy) || {};
    const sleepLast = last(doc.sleepBy) || {};
    const rhr7 = s.rhr_7d || T.mean(T.series(doc.rhrBy, 8).slice(0, 7));
    const bbToday = (doc.bbBy[T.iso(T.today())] || {});
    return {
      bb: s.body_battery != null ? s.body_battery : bbToday.latest,
      bbHigh: s.body_battery_high != null ? s.body_battery_high : bbToday.high,
      hrv: h.last_night != null ? h.last_night : hrvLast.last_night,
      hrvLow: h.low != null ? h.low : hrvLast.low,
      hrvHigh: h.high != null ? h.high : hrvLast.high,
      hrvStatus: h.status || hrvLast.status,
      sleepS: sl.duration_s != null ? sl.duration_s : sleepLast.duration_s,
      sleepScore: sl.score != null ? sl.score : sleepLast.score,
      sleep: Object.keys(sl).length && sl.duration_s != null ? sl : sleepLast,
      rhr: s.rhr != null ? s.rhr : last(doc.rhrBy),
      rhr7: rhr7,
      stress: s.stress != null ? s.stress : last(doc.stressBy),
      fitAge: (doc.fitness_age || {}).fitness_age,
      age: (doc.fitness_age || {}).chronological_age,
    };
  };

  T.form = (doc) => (doc.form.length ? doc.form[doc.form.length - 1] : null);

  // Flags: [{key,label,level:'green'|'amber'|'red',text}]
  T.flags = function (doc) {
    const v = doc.vitals, f = T.form(doc), out = [];
    if (v.hrv != null) {
      const below = v.hrvLow != null && v.hrv < v.hrvLow, above = v.hrvHigh != null && v.hrv > v.hrvHigh;
      out.push({ key: "hrv", label: "HRV", level: below ? "red" : above ? "amber" : "green",
        text: `HRV ${Math.round(v.hrv)} ms ` + (v.hrvLow != null ? (below ? `below your ${v.hrvLow}–${v.hrvHigh} baseline` : above ? `above your ${v.hrvLow}–${v.hrvHigh} baseline` : `inside your ${v.hrvLow}–${v.hrvHigh} baseline`) : "") });
    }
    if (v.sleepScore != null || v.sleepS != null) {
      const sc = v.sleepScore, lvl = sc == null ? "amber" : sc < 60 ? "red" : sc < 75 ? "amber" : "green";
      out.push({ key: "sleep", label: "Sleep", level: lvl, text: `Slept ${T.dur(v.sleepS)}` + (sc != null ? `, score ${sc}` : "") });
    }
    if (v.rhr != null && v.rhr7 != null) {
      const diff = v.rhr - v.rhr7;
      out.push({ key: "rhr", label: "Resting HR", level: diff >= 3 ? "red" : diff >= 1 ? "amber" : "green",
        text: `Resting HR ${v.rhr} bpm vs ${Math.round(v.rhr7)} 7-day average (${diff >= 0 ? "+" : ""}${Math.round(diff)})` });
    }
    if (f) {
      const lvl = f.form < -15 ? "red" : f.form < -5 ? "amber" : "green";
      out.push({ key: "form", label: "Form", level: lvl, text: `Form ${f.form >= 0 ? "+" : ""}${f.form.toFixed(0)} (fitness ${f.fitness.toFixed(0)}, fatigue ${f.fatigue.toFixed(0)})` });
    }
    if (v.bb != null) {
      out.push({ key: "bb", label: "Body Battery", level: v.bb < 25 ? "red" : v.bb < 40 ? "amber" : "green", text: `Body Battery ${v.bb}` });
    }
    return out;
  };

  // Recovery verdict per the Recovery Nerd rules.
  T.verdict = function (doc) {
    const v = doc.vitals, reasons = [];
    if (v.hrv != null && v.hrvLow != null && v.hrv < v.hrvLow) reasons.push("HRV below baseline");
    if (v.sleepScore != null && v.sleepScore < 60) reasons.push("Sleep score under 60");
    if (v.rhr != null && v.rhr7 != null && v.rhr > v.rhr7) reasons.push("Resting HR above 7-day average");
    if (v.bb != null && v.bb < 40) reasons.push("Body Battery under 40");
    const call = reasons.length >= 2 ? "Easy day" : reasons.length === 1 ? "Train, but cap it" : "Go hard";
    const light = reasons.length >= 2 ? "red" : reasons.length === 1 ? "amber" : "green";
    return { call, light, reasons };
  };

  T.goal = function (doc) {
    const c = doc.cfg || {};
    const name = T.qs.get("race") || c.goal_race || "Goal race";
    const date = T.qs.get("date") || c.goal_date;
    const days = date ? T.daysBetween(T.today(), T.d(date)) : null;
    return { name, date, days };
  };

  // Weekly totals (Monday weeks), newest last.
  T.weekly = function (acts, weeks, value) {
    const start = T.monday(T.today());
    const out = [];
    for (let i = weeks - 1; i >= 0; i--) {
      const w = new Date(start - i * 7 * DAY), key = T.iso(w), end = T.iso(new Date(w.getTime() + 6 * DAY));
      const inWeek = acts.filter((a) => a.date >= key && a.date <= end);
      out.push({ week: key, value: value(inWeek), acts: inWeek });
    }
    return out;
  };

  // ---------- tiny SVG charts ----------
  T.spark = function (vals, color, w = 120, h = 30) {
    const v = vals.map((x, i) => [i, x]).filter((p) => p[1] != null);
    if (v.length < 2) return `<svg width="${w}" height="${h}"></svg>`;
    const xs = vals.length - 1, lo = Math.min(...v.map((p) => p[1])), hi = Math.max(...v.map((p) => p[1])), r = hi - lo || 1;
    const pts = v.map((p) => `${(p[0] / xs) * (w - 4) + 2},${h - 3 - ((p[1] - lo) / r) * (h - 6)}`).join(" ");
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline fill="none" stroke="${color}" stroke-width="1.6" points="${pts}"/></svg>`;
  };

  T.bars = function (vals, opt) {
    const o = Object.assign({ w: 500, h: 140, color: "#4ade80", labels: [], colors: null, fmt: (x) => x.toFixed(0), text: "#999" }, opt || {});
    const max = Math.max(1, ...vals.map((x) => x || 0)), n = vals.length, bw = (o.w - 10) / n;
    let s = `<svg viewBox="0 0 ${o.w} ${o.h + 18}" width="100%">`;
    vals.forEach((x, i) => {
      const bh = ((x || 0) / max) * (o.h - 14), X = 5 + i * bw + bw * 0.12, c = (o.colors && o.colors[i]) || o.color;
      s += `<rect x="${X}" y="${o.h - bh}" width="${bw * 0.76}" height="${bh}" rx="2" fill="${c}"/>`;
      if (x) s += `<text x="${X + bw * 0.38}" y="${o.h - bh - 3}" font-size="9" text-anchor="middle" fill="${o.text}">${o.fmt(x)}</text>`;
      if (o.labels[i]) s += `<text x="${X + bw * 0.38}" y="${o.h + 12}" font-size="8.5" text-anchor="middle" fill="${o.text}">${o.labels[i]}</text>`;
    });
    return s + "</svg>";
  };

  T.lines = function (series, opt) {
    // series: [{vals, color, width, fill}] all same length
    const o = Object.assign({ w: 800, h: 200, zero: false, labels: [], text: "#999", band: null }, opt || {});
    const all = series.flatMap((s) => s.vals).filter((x) => x != null).concat(o.band ? [o.band[0], o.band[1]] : []);
    if (!all.length) return "";
    let lo = Math.min(...all), hi = Math.max(...all);
    if (o.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
    const n = series[0].vals.length, X = (i) => 30 + (i / (n - 1)) * (o.w - 40), Y = (v) => 8 + (1 - (v - lo) / (hi - lo)) * (o.h - 24);
    let s = `<svg viewBox="0 0 ${o.w} ${o.h}" width="100%">`;
    if (o.band) s += `<rect x="30" width="${o.w - 40}" y="${Y(o.band[1])}" height="${Y(o.band[0]) - Y(o.band[1])}" fill="${o.bandColor || "rgba(120,200,160,.15)"}"/>`;
    for (let g = 0; g <= 4; g++) {
      const v = lo + ((hi - lo) * g) / 4;
      s += `<line x1="30" x2="${o.w - 10}" y1="${Y(v)}" y2="${Y(v)}" stroke="${o.grid || "rgba(128,128,128,.15)"}"/><text x="26" y="${Y(v) + 3}" font-size="9" text-anchor="end" fill="${o.text}">${o.fmt ? o.fmt(v) : v.toFixed(0)}</text>`;
    }
    if (o.zero) s += `<line x1="30" x2="${o.w - 10}" y1="${Y(0)}" y2="${Y(0)}" stroke="${o.text}" stroke-dasharray="3 3"/>`;
    series.forEach((se) => {
      let path = "", pen = false;
      se.vals.forEach((v, i) => { if (v == null) { pen = false; return; } path += (pen ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1); pen = true; });
      if (se.fill) s += `<path d="${path}L${X(n - 1)},${Y(Math.max(lo, 0))}L${X(0)},${Y(Math.max(lo, 0))}Z" fill="${se.fill}"/>`;
      if (se.width !== 0) s += `<path d="${path}" fill="none" stroke="${se.color}" stroke-width="${se.width || 1.8}"/>`;
      if (se.dots) se.vals.forEach((v, i) => { if (v != null) s += `<circle cx="${X(i)}" cy="${Y(v)}" r="2.4" fill="${se.color}"/>`; });
    });
    (o.labels || []).forEach((l, i) => { if (l) s += `<text x="${X(i)}" y="${o.h - 3}" font-size="9" text-anchor="${i === n - 1 && n > 1 ? "end" : "middle"}" fill="${o.text}">${l}</text>`; });
    return s + "</svg>";
  };

  T.esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  window.T = T;
})();
