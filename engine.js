/* engine.js: the "why did it move?" analysis for any table of rows, in the browser.
   Same maths as engine/analyze_csv.py (Python) and sql/02-03 (PostgreSQL):
   period totals -> change -> z-score vs the previous 6 changes -> exact segment contributions. */
(function (root) {
  const isBlank = (v) => v === null || v === undefined || String(v).trim() === "";
  const DATE_RE = /\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\/\d{1,2}\/\d{2,4}/;

  function parseDate(v) {
    const s = String(v).trim();
    let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (m) return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})/);           // assume month/day/year, as pandas does
    if (m) { const y = +m[3] < 100 ? 2000 + +m[3] : +m[3]; return new Date(Date.UTC(y, +m[1] - 1, +m[2])); }
    const t = Date.parse(s); return isNaN(t) ? null : new Date(t);
  }
  const toNum = (v) => { if (isBlank(v)) return NaN; const n = Number(String(v).replace(/,/g, "")); return isFinite(n) ? n : NaN; };

  // ---------------------------------------------------------------- 1. profile
  function profile(rows, columns) {
    return columns.map((c) => {
      const vals = rows.map((r) => r[c]).filter((v) => !isBlank(v));
      const distinct = new Set(vals.map(String)).size;
      let type = "text";
      if (vals.length) {
        const sample = vals.length > 5000 ? vals.filter((_, i) => i % Math.ceil(vals.length / 5000) === 0) : vals;
        const dateShare = sample.filter((v) => DATE_RE.test(String(v)) && parseDate(v)).length / sample.length;
        const numShare = sample.filter((v) => !isNaN(toNum(v))).length / sample.length;
        if (dateShare > 0.9) type = "date"; else if (numShare > 0.95) type = "number"; else if (distinct <= 100) type = "category";
      }
      const info = { column: c, type, missing_pct: +(100 * (1 - vals.length / Math.max(rows.length, 1))).toFixed(1), distinct };
      if (type === "number") { const ns = vals.map(toNum).filter((n) => !isNaN(n));
        info.min = Math.min(...ns); info.max = Math.max(...ns); info.mean = +(ns.reduce((a, b) => a + b, 0) / ns.length).toFixed(2); }
      if (type === "date") { const ds = vals.map(parseDate).filter(Boolean).map((d) => d.getTime());
        info.min = new Date(Math.min(...ds)).toISOString().slice(0, 10); info.max = new Date(Math.max(...ds)).toISOString().slice(0, 10); }
      if (type === "category" || type === "text") {
        const cnt = {}; vals.forEach((v) => { cnt[v] = (cnt[v] || 0) + 1; });
        info.top_values = Object.entries(cnt).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k]) => k);
      }
      return info;
    });
  }

  function periodKey(d, period) {
    const y = d.getUTCFullYear(), m = d.getUTCMonth();
    if (period === "Q") return `${y}-Q${Math.floor(m / 3) + 1}`;
    if (period === "W") { const day = (d.getUTCDay() + 6) % 7; const mon = new Date(d.getTime() - day * 86400000); return mon.toISOString().slice(0, 10); }
    return `${y}-${String(m + 1).padStart(2, "0")}`;
  }

  // ---------------------------------------------------------------- 2-4. analyze
  function analyze(rows, o) {
    const { date, metric, agg, dims, period = "M", better = "up" } = o;
    const recs = [];
    for (const r of rows) {
      const d = isBlank(r[date]) ? null : parseDate(r[date]); if (!d) continue;
      let x = metric === "rows" ? 1 : toNum(r[metric]);
      if (agg === "mean" && isNaN(x)) continue;
      if (isNaN(x)) x = 0;
      recs.push({ p: periodKey(d, period), x, r });
    }
    const tot = {};
    recs.forEach(({ p, x }) => { (tot[p] ||= { n: 0, s: 0 }); tot[p].n++; tot[p].s += x; });
    let periods = Object.keys(tot).sort();
    const ns = periods.map((p) => tot[p].n).sort((a, b) => a - b);
    const median = ns.length % 2 ? ns[(ns.length - 1) / 2] : (ns[ns.length / 2 - 1] + ns[ns.length / 2]) / 2;
    periods = periods.filter((p) => tot[p].n >= 0.1 * median);       // drop thin edge periods
    const keep = new Set(periods);
    const val = (t) => agg === "sum" ? t.s : agg === "count" ? t.n : t.s / t.n;
    const value = periods.map((p) => val(tot[p]));
    const change = value.map((v, i) => (i ? v - value[i - 1] : null));
    const z = value.map((_, i) => {
      const prev = change.slice(Math.max(1, i - 6), i).filter((c) => c !== null);
      if (prev.length < 4) return null;
      const mean = prev.reduce((a, b) => a + b, 0) / prev.length;
      const sd = Math.sqrt(prev.reduce((a, b) => a + (b - mean) ** 2, 0) / (prev.length - 1));
      return sd > 0 ? (change[i] - mean) / sd : null;
    });

    const seg = {};                       // dim -> period -> segment -> {n, s}
    dims.forEach((dim) => { seg[dim] = {}; });
    recs.forEach(({ p, x, r }) => {
      if (!keep.has(p)) return;
      dims.forEach((dim) => { const s = isBlank(r[dim]) ? "(blank)" : String(r[dim]);
        const pp = (seg[dim][p] ||= {}); const t = (pp[s] ||= { n: 0, s: 0 }); t.n++; t.s += x; });
    });

    const hist = {}, raw = {};
    for (let i = 1; i < periods.length; i++) {
      const p1 = periods[i], p0 = periods[i - 1];
      dims.forEach((dim) => {
        const a = seg[dim][p1] || {}, b = seg[dim][p0] || {};
        new Set([...Object.keys(a), ...Object.keys(b)]).forEach((s) => {
          const A = a[s] || { n: 0, s: 0 }, B = b[s] || { n: 0, s: 0 };
          let c, now, before;
          if (agg === "sum") { c = A.s - B.s; now = A.s; before = B.s; }
          else if (agg === "count") { c = A.n - B.n; now = A.n; before = B.n; }
          else { c = A.s / tot[p1].n - B.s / tot[p0].n; now = A.n ? A.s / A.n : null; before = B.n ? B.s / B.n : null; }
          ((hist[dim + "\u0000" + s] ||= {})[p1] = c);
          ((raw[p1 + "\u0000" + dim] ||= []).push({ s, c, now, before }));
        });
      });
    }
    const segZ = (dim, s, p) => {
      const h = hist[dim + "\u0000" + s] || {}; const i = periods.indexOf(p);
      const prev = periods.slice(Math.max(1, i - 6), i).map((q) => h[q] || 0);
      if (prev.length < 4) return null;
      const mean = prev.reduce((a, b) => a + b, 0) / prev.length;
      const sd = Math.sqrt(prev.reduce((a, b) => a + (b - mean) ** 2, 0) / (prev.length - 1));
      const floor = 0.05 * Math.max(...prev.map(Math.abs), Math.abs(h[p] || 0));
      return +(((h[p] || 0) - mean) / Math.max(sd, floor, 1e-9)).toFixed(2);
    };
    const r2 = (x, d = 2) => (x === null || x === undefined || !isFinite(x) ? null : +(+x).toFixed(d));
    const label = metric === "rows" ? "Number of rows" : { sum: "Total", count: "Count of", mean: "Average" }[agg] + " " + metric;

    const packets = [];
    for (let i = 1; i < periods.length; i++) {
      const p1 = periods[i], p0 = periods[i - 1], ch = change[i];
      const pk = { metric: "custom", metric_label: label, unit: metric, better_when: better, month: p1,
        value: r2(value[i], 4), previous_value: r2(value[i - 1], 4), change: r2(ch, 4),
        change_pct: value[i - 1] ? r2(100 * ch / value[i - 1]) : null, z_score: z[i] === null ? null : r2(z[i]) };
      pk.is_unusual = pk.z_score !== null && Math.abs(pk.z_score) >= 2;
      if (agg === "sum" && metric !== "rows") {
        const A = tot[p1], B = tot[p0];
        pk.rows = A.n; pk.previous_rows = B.n;
        pk.volume_effect = r2((A.n - B.n) * (B.s / B.n)); pk.value_effect = r2(A.n * (A.s / A.n - B.s / B.n));
      }
      pk.drivers = {};
      dims.forEach((dim) => {
        const items = (raw[p1 + "\u0000" + dim] || []).filter((t) => t.c !== 0);
        const mk = (t) => ({ segment: t.s, contribution: r2(t.c, 4), share_of_change_pct: ch ? r2(100 * t.c / ch, 1) : null,
          segment_value_now: r2(t.now), segment_value_before: r2(t.before), segment_z_score: segZ(dim, t.s, p1) });
        const by = (a, b) => Math.abs(b.c) - Math.abs(a.c);
        pk.drivers[dim] = { pushed_with_the_change: items.filter((t) => (t.c > 0) === (ch > 0)).sort(by).slice(0, 3).map(mk),
                            pushed_against_the_change: items.filter((t) => (t.c > 0) !== (ch > 0)).sort(by).slice(0, 3).map(mk) };
      });
      pk.segment_alerts = [];
      Object.entries(pk.drivers).forEach(([dim, dd]) => [...dd.pushed_with_the_change, ...dd.pushed_against_the_change].forEach((it) => {
        if (it.segment_z_score !== null && Math.abs(it.segment_z_score) >= 4 && it.share_of_change_pct !== null && Math.abs(it.share_of_change_pct) >= 20)
          pk.segment_alerts.push({ dimension: dim, segment: it.segment, segment_z_score: it.segment_z_score, share_of_change_pct: it.share_of_change_pct, contribution: it.contribution });
      }));
      packets.push(pk);
    }
    const series = periods.map((p, i) => ({ metric: "custom", month: p, value: value[i], change: change[i],
      change_pct: i && value[i - 1] ? r2(100 * change[i] / value[i - 1]) : null, z_score: z[i] === null ? null : r2(z[i]) }));
    return { label, periods, series, packets, rowsUsed: recs.length };
  }

  const api = { profile, analyze, parseDate, toNum };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.Engine = api;
})(typeof window !== "undefined" ? window : globalThis);
