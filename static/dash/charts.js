/* Minimal dependency-free SVG charts (no CDN needed). Charts re-render only when their data signature changes. */
(function () {
  const C = { blue: '#2F81F7', red: '#EF4444', green: '#22C55E', amber: '#F5B301', cyan: '#38BDF8', grey: '#64748B' };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let tz = 'UTC';
  const fmtT = (t, mode) => {
    const o = mode === 'time' ? { hour: '2-digit', minute: '2-digit', hour12: false } : mode === 'day' ? { day: '2-digit', month: 'short' } : { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false };
    try { return new Intl.DateTimeFormat('en-GB', { ...o, timeZone: tz }).format(new Date(t * 1000)); } catch (e) { return new Date(t * 1000).toISOString().slice(11, 16); }
  };
  const nice = (min, max, n = 4) => {
    if (min === max) { min -= 1; max += 1; }
    const step0 = (max - min) / n, mag = Math.pow(10, Math.floor(Math.log10(step0))), r = step0 / mag;
    const step = (r < 1.5 ? 1 : r < 3 ? 2 : r < 7 ? 5 : 10) * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(6));
    return { lo, hi, ticks };
  };
  // Skip the redraw when the data is unchanged (no flicker, no duplicate points on repeated polls).
  const same = (el, x) => { const j = JSON.stringify(x); if (el._sig === j) return true; el._sig = j; return false; };
  const empty = (el, msg) => { el._sig = null; el.innerHTML = `<div class="empty">${esc(msg || 'No data for this selection')}</div>`; };
  const tip = el => { let t = el.querySelector('.tip'); if (!t) { el.style.position = 'relative'; t = document.createElement('div'); t.className = 'tip'; el.appendChild(t); } return t; };

  function line(el, o) {
    const S = (o.series || []).filter(s => s.data && s.data.length);
    if (!S.length) return empty(el, o.empty);
    if (same(el, o)) return;
    const W = 640, H = o.height || 230, m = { l: 44, r: S.some(s => s.axis === 'r') ? 44 : 14, t: 10, b: 24 };
    const all = S.flatMap(s => s.data);
    const t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0])) || t0 + 1;
    const ax = side => {
      const v = S.filter(s => (s.axis || 'l') === side).flatMap(s => s.data.map(p => p[1]));
      if (!v.length) return null;
      let lo = Math.min(...v), hi = Math.max(...v);
      if (o.thresholds && side === 'l') o.thresholds.forEach(th => { hi = Math.max(hi, th.value); });
      if (side === 'l' && lo > 0 && (hi - lo) / hi > .35) lo = 0;
      return nice(lo, hi);
    };
    const A = { l: ax('l'), r: ax('r') };
    if (!A.l) A.l = A.r;
    const X = t => m.l + (t - t0) / ((t1 - t0) || 1) * (W - m.l - m.r);
    const Y = (v, side) => { const a = A[side || 'l'] || A.l; return H - m.b - (v - a.lo) / ((a.hi - a.lo) || 1) * (H - m.t - m.b); };
    let g = '<g class="grid">' + A.l.ticks.map(v => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/>`).join('') + '</g>';
    g += A.l.ticks.map(v => `<text x="${m.l - 6}" y="${Y(v) + 3}" text-anchor="end">${+v.toFixed(2)}</text>`).join('');
    if (A.r) g += A.r.ticks.map(v => `<text x="${W - m.r + 6}" y="${Y(v, 'r') + 3}">${+v.toFixed(2)}</text>`).join('');
    const span = t1 - t0;
    for (let i = 0; i < 5; i++) { const t = t0 + span * i / 4; g += `<text x="${X(t)}" y="${H - 6}" text-anchor="${i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}">${fmtT(t, span > 2 * 86400 ? 'day' : 'time')}</text>`; }
    (o.thresholds || []).forEach(th => { g += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(th.value)}" y2="${Y(th.value)}" stroke="${th.color || C.red}" stroke-dasharray="5 4"/><text x="${W - m.r}" y="${Y(th.value) - 3}" text-anchor="end" style="fill:${th.color || C.red}">${esc(th.label)}</text>`; });
    S.forEach(s => {
      const sd = s.axis || 'l', pts = s.data.map(p => `${X(p[0]).toFixed(1)},${Y(p[1], sd).toFixed(1)}`);
      if (s.fill) g += `<polygon points="${X(s.data[0][0])},${H - m.b} ${pts.join(' ')} ${X(s.data[s.data.length - 1][0])},${H - m.b}" fill="${s.color}" opacity=".18"/>`;
      g += `<polyline points="${pts.join(' ')}" fill="none" stroke="${s.color}" stroke-width="1.8" stroke-linejoin="round"/>`;
    });
    const leg = S.length > 1 ? `<div class="legend">${S.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join('')}</div>` : '';
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label || 'chart')}">${g}<line class="xh" y1="${m.t}" y2="${H - m.b}" stroke="#fff" stroke-opacity=".35" style="display:none"/></svg>${leg}`;
    const svg = el.querySelector('svg'), xh = svg.querySelector('.xh'), tp = tip(el);
    svg.addEventListener('mousemove', e => {
      const r = svg.getBoundingClientRect(), px = (e.clientX - r.left) / r.width * W, t = t0 + (px - m.l) / (W - m.l - m.r) * (t1 - t0);
      let html = '', ts = null;
      S.forEach(s => { let b = s.data[0]; s.data.forEach(p => { if (Math.abs(p[0] - t) < Math.abs(b[0] - t)) b = p; }); ts = b[0]; html += `<div><i style="display:inline-block;width:8px;height:8px;background:${s.color};margin-right:5px"></i>${esc(s.name)}: <b>${+b[1].toFixed(2)}</b> ${esc(s.unit || '')}</div>`; });
      xh.setAttribute('x1', X(ts)); xh.setAttribute('x2', X(ts)); xh.style.display = '';
      tp.innerHTML = `<div style="color:#8CA0B8">${fmtT(ts)}</div>${html}`; tp.style.display = 'block';
      tp.style.left = Math.min(el.clientWidth - 200, Math.max(0, e.clientX - el.getBoundingClientRect().left + 12)) + 'px'; tp.style.top = '8px';
    });
    svg.addEventListener('mouseleave', () => { xh.style.display = 'none'; tp.style.display = 'none'; });
  }

  function bars(el, o) {
    const I = o.items || [];
    if (!I.length || I.every(i => i.value == null)) return empty(el, o.empty);
    if (same(el, o)) return;
    const W = 640, H = o.height || 230, m = { l: 44, r: 14, t: 18, b: 34 };
    const a = nice(0, Math.max(...I.map(i => i.value || 0), o.threshold ? o.threshold.value : 0));
    const Y = v => H - m.b - v / (a.hi || 1) * (H - m.t - m.b), bw = (W - m.l - m.r) / I.length;
    let g = '<g class="grid">' + a.ticks.map(v => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/>`).join('') + '</g>' + a.ticks.map(v => `<text x="${m.l - 6}" y="${Y(v) + 3}" text-anchor="end">${v}</text>`).join('');
    I.forEach((i, k) => {
      const x = m.l + k * bw + bw * .15, w = bw * .7;
      if (i.value == null) g += `<text x="${x + w / 2}" y="${H - m.b - 4}" text-anchor="middle">n/a</text>`;
      else g += `<rect x="${x}" y="${Y(i.value)}" width="${w}" height="${H - m.b - Y(i.value)}" rx="2" fill="${i.color || C.blue}"><title>${esc(i.label)}: ${i.value}${esc(o.unit || '')}</title></rect><text x="${x + w / 2}" y="${Y(i.value) - 4}" text-anchor="middle" style="fill:#e6eef8">${i.value}</text>`;
      g += `<text x="${x + w / 2}" y="${H - m.b + 13}" text-anchor="middle">${esc(String(i.label).slice(0, 12))}</text>`;
    });
    if (o.threshold) g += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(o.threshold.value)}" y2="${Y(o.threshold.value)}" stroke="#fff" stroke-dasharray="5 4"/><text x="${W - m.r}" y="${Y(o.threshold.value) - 4}" text-anchor="end" style="fill:#fff">${esc(o.threshold.label)}</text>`;
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label || 'bar chart')}">${g}</svg>`;
  }

  function hbars(el, o) {
    const I = o.items || [];
    if (!I.length) return empty(el, o.empty);
    if (same(el, o)) return;
    const mx = Math.max(...I.map(i => i.value || 0), 0.0001);
    el.innerHTML = I.map(i => `<div class="hb"><span title="${esc(i.label)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(i.label)}</span><span class="bar"><i class="${i.cls || ''}" style="width:${i.value == null ? 0 : Math.min(100, i.value / (i.max || mx) * 100)}%"></i></span><span style="text-align:right">${esc(i.text ?? (i.value == null ? 'n/a' : i.value))}</span></div>`).join('');
  }

  function donut(el, o) {
    const I = (o.items || []).filter(i => i.value > 0), tot = I.reduce((a, i) => a + i.value, 0);
    if (!I.length) return empty(el, o.empty);
    if (same(el, o)) return;
    const pal = [C.blue, C.amber, '#FACC15', C.green, '#94A3B8', C.cyan, '#A78BFA', '#FB7185'];
    let a0 = -Math.PI / 2, paths = '';
    I.forEach((i, k) => {
      const a1 = a0 + i.value / tot * 2 * Math.PI, R = 70, r = 44, L = a1 - a0 > Math.PI ? 1 : 0, p = (a, rad) => `${90 + rad * Math.cos(a)},${90 + rad * Math.sin(a)}`;
      paths += `<path d="M${p(a0, R)} A${R},${R} 0 ${L} 1 ${p(a1 - .0001, R)} L${p(a1 - .0001, r)} A${r},${r} 0 ${L} 0 ${p(a0, r)}Z" fill="${pal[k % pal.length]}"><title>${esc(i.label)}: ${i.value} ${esc(o.unit || '')}</title></path>`; a0 = a1;
    });
    el.innerHTML = `<div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap"><svg viewBox="0 0 180 180" style="width:150px;flex:none" role="img" aria-label="${esc(o.label || 'distribution')}">${paths}<text x="90" y="88" text-anchor="middle" style="fill:#fff;font:700 18px sans-serif">${+tot.toFixed(1)}</text><text x="90" y="104" text-anchor="middle">${esc(o.unit || '')}</text></svg><div class="legend" style="flex-direction:column;gap:5px;flex:1;min-width:120px">${I.map((i, k) => `<span><i style="background:${pal[k % pal.length]}"></i>${esc(i.label)} <b style="color:#e6eef8">${Math.round(i.value / tot * 100)}%</b></span>`).join('')}</div></div>`;
  }

  function scatter(el, o) {
    const P = o.points || [];
    if (P.length < 2) return empty(el, o.empty || 'Not enough paired data');
    if (same(el, o)) return;
    const W = 640, H = 230, m = { l: 48, r: 14, t: 10, b: 34 };
    const xa = nice(0, Math.max(...P.map(p => p[0]))), ya = nice(0, Math.max(...P.map(p => p[1])));
    const X = v => m.l + v / (xa.hi || 1) * (W - m.l - m.r), Y = v => H - m.b - v / (ya.hi || 1) * (H - m.t - m.b);
    let g = '<g class="grid">' + ya.ticks.map(v => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/>`).join('') + '</g>' + ya.ticks.map(v => `<text x="${m.l - 6}" y="${Y(v) + 3}" text-anchor="end">${v}</text>`).join('') + xa.ticks.map(v => `<text x="${X(v)}" y="${H - m.b + 13}" text-anchor="middle">${v}</text>`).join('');
    g += P.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3" fill="${C.blue}" opacity=".75"><title>${p[0]} people, ${p[1]} kW</title></circle>`).join('');
    const r = o.regression;
    if (r && r.show) g += `<line x1="${X(0)}" y1="${Y(r.intercept)}" x2="${X(xa.hi)}" y2="${Y(r.slope * xa.hi + r.intercept)}" stroke="${C.red}" stroke-width="1.6"/>`;
    g += `<text x="${(W + m.l) / 2}" y="${H - 4}" text-anchor="middle">${esc(o.xLabel || '')}</text><text transform="translate(11 ${H / 2}) rotate(-90)" text-anchor="middle">${esc(o.yLabel || '')}</text>`;
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="scatter plot">${g}</svg>${r ? `<div class="legend">r = ${r.r}${r.show ? '' : ' (weak correlation, trendline hidden)'}</div>` : '<div class="legend">Trendline needs ≥10 paired samples</div>'}`;
  }

  function gauge(el, o) {
    if (o.value == null) return empty(el, 'Comfort score unavailable (no inputs)');
    if (same(el, o)) return;
    const v = Math.max(0, Math.min(100, o.value)), a = Math.PI * (1 - v / 100), col = v >= 80 ? C.green : v >= 60 ? C.amber : C.red;
    const pt = (ang, r) => `${100 + r * Math.cos(ang)},${100 - r * Math.sin(ang)}`;
    el.innerHTML = `<svg viewBox="0 0 200 120" style="max-width:240px" role="img" aria-label="comfort score ${v}"><path d="M${pt(Math.PI, 80)} A80,80 0 0 1 ${pt(0, 80)}" fill="none" stroke="#22344a" stroke-width="14"/><path d="M${pt(Math.PI, 80)} A80,80 0 0 1 ${pt(a, 80)}" fill="none" stroke="${col}" stroke-width="14" stroke-linecap="round"/><text x="100" y="92" text-anchor="middle" style="fill:#fff;font:700 28px sans-serif">${v}</text><text x="100" y="110" text-anchor="middle">/ 100</text></svg>`;
  }

  window.SBChart = { line, bars, hbars, donut, scatter, gauge, esc, C, fmtT, setTz: z => { tz = z || 'UTC'; } };
})();
