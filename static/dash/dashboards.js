/* Smart Building analytics workspace. The Python backend is the only source of readings; this file renders them. */
(function () {
  'use strict';
  const { esc, C } = SBChart;
  const app = document.getElementById('app');
  const PAGE = app.dataset.page, POLL = +app.dataset.poll || 4000, ADMIN = app.dataset.admin === '1';
  const $ = (s, r = document) => r.querySelector(s);
  const FKEYS = ['building_id', 'floor_id', 'room_id', 'range', 'start', 'end', 'severity', 'status'];
  const S = { meta: null, f: {}, data: null, lastOk: null, failing: false, plan: null, tz: 'UTC', clock: null, labels: true };
  const q0 = new URLSearchParams(location.search);
  FKEYS.forEach(k => { if (q0.get(k)) S.f[k] = q0.get(k); });
  if (!S.f.range) S.f.range = '24h';

  const ICON = {
    temp: '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/>', hum: '<path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z"/>',
    co2: '<path d="M7 18a4 4 0 0 1-.6-7.9A6 6 0 0 1 18 9.5 4 4 0 0 1 17 18z"/>', occ: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.5-4 3-6 6.5-6s6 2 6.5 6"/><circle cx="17" cy="9" r="2.5"/>',
    pow: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>', water: '<path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z"/><path d="M9 14a3 3 0 0 0 3 3"/>',
    lux: '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>', shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    cost: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3 3 7 3s7-1.3 7-3v-6"/>', gauge: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="m12 17 4-5"/>', leaf: '<path d="M5 19C5 9 11 4 20 4c0 9-5 15-15 15zM5 19l8-8"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', door: '<path d="M6 3h12v18H6zM14 12h.01"/>', rooms: '<rect x="3" y="3" width="18" height="18" rx="1"/><path d="M3 12h9M12 3v9"/>', pm: '<path d="M3 8h10a3 3 0 1 0-3-3M3 12h15a3 3 0 1 1-3 3M3 16h7"/>',
    flame: '<path d="M12 22c4 0 7-3 7-7 0-3-2-5-3-8-2 2-2 4-3 4 0-3-1-6-4-8 0 5-4 7-4 12 0 4 3 7 7 7z"/>', gas: '<path d="M3 8h10a3 3 0 1 0-3-3M3 12h15a3 3 0 1 1-3 3M3 16h7"/>', dev: '<rect x="4" y="6" width="16" height="12" rx="2"/><path d="M8 10h.01M12 10h.01"/>'
  };
  const ic = (k, col) => `<span class="ic"><svg viewBox="0 0 24 24" stroke="${col}">${ICON[k]}</svg></span>`;
  const SRC = { live: 'LIVE', simulated: 'SIMULATED', mixed: 'MIXED', stale: 'STALE', no_data: 'NO DATA', not_configured: 'NOT CONFIGURED', none: 'NO DEVICES', unavailable: 'UNAVAILABLE', capacity_not_configured: 'NO CAPACITY' };
  const PROVENANCE = { live: 1, simulated: 1, mixed: 1 };
  /* With data-source labels switched off (Settings), provenance badges are not rendered at all. */
  const badge = (s, txt) => (!S.labels && PROVENANCE[s] && !txt) ? '' : `<span class="badge b-${esc(s)}">${esc(txt || SRC[s] || s)}</span>`;
  const when = (iso, o = { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) => { if (!iso) return '–'; const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso.replace(' ', 'T') + 'Z'); return isNaN(d) ? iso : tzTime(d, o); };
  const num = (v, d = 1) => v == null ? 'n/a' : (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('en-GB') : +(+v).toFixed(d));
  const qs = (extra = {}, drop = []) => { const p = new URLSearchParams(); Object.entries({ ...S.f, ...extra }).forEach(([k, v]) => { if (v != null && v !== '' && !drop.includes(k)) p.set(k, v); }); return p.toString(); };
  const tzTime = (d, o) => { try { return new Intl.DateTimeFormat('en-GB', { timeZone: S.tz, ...o }).format(d); } catch (e) { return d.toISOString(); } };
  const ago = iso => { if (!iso) return 'never'; const s = Math.max(0, (Date.now() - new Date(iso)) / 1000); return s < 90 ? Math.round(s) + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };

  async function api(path, opt) {
    const r = await fetch(path, { credentials: 'same-origin', headers: { Accept: 'application/json' }, ...opt });
    if (r.status === 401) { location.href = '/admin-login?next=' + encodeURIComponent(location.pathname + location.search); throw new Error('auth'); }
    if (!r.ok) { let d = r.statusText; try { d = (await r.json()).detail || d; } catch (e) { } throw new Error(typeof d === 'string' ? d : 'Request failed'); }
    return r.json();
  }
  const send = (method, path, body) => api(path, { method, headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });

  /* ---------------- KPI cards ---------------- */
  function kpiCard(label, k, o = {}) {
    const col = o.color || C.cyan, unit = o.unit ?? k.unit ?? '';
    const val = k.value == null ? `<span style="font-size:14px;color:var(--muted)">${SRC[k.state] || '—'}</span>` : `${num(k.value, o.d ?? 1)} <small>${esc(unit)}</small>`;
    const c = k.comparison;
    let cmp = '';
    if (c && c.available) {
      const up = c.diff > 0, flat = c.diff === 0;
      const txt = c.pct != null ? `${c.pct > 0 ? '+' : ''}${c.pct}%` : `${up ? '+' : ''}${num(c.diff, 2)} ${esc(c.unit || '')}`;
      cmp = `<div class="delta" title="${esc(c.basis || 'vs previous equivalent period')}">${flat ? '–' : up ? '▲' : '▼'} ${txt} vs prev. period</div>`;
    }
    const st = k.status && k.status !== 'normal' ? ' ' + badge(k.status) : '';
    const src = ['live', 'simulated', 'mixed'].includes(k.state) ? badge(k.state) : '';
    return `<div class="card">${ic(o.icon, col)}<div style="min-width:0"><div class="v">${val}</div><div class="l">${esc(label)} ${src}${st}</div>${cmp}</div></div>`;
  }
  const statCard = (label, state, text, icon, col, note) => `<div class="card">${ic(icon, col)}<div><div class="v" style="font-size:17px;color:${state === 'critical' ? 'var(--crit)' : state === 'warning' ? 'var(--warn)' : state === 'normal' ? 'var(--ok)' : 'var(--muted)'}">${esc(text)}</div><div class="l">${esc(label)} ${badge(state)}</div>${note ? `<div class="delta">${esc(note)}</div>` : ''}</div></div>`;

  /* ---------------- floor-plan component (shared by Overview, Occupancy, Safety, Floor Plan) ---------------- */
  const HEAT = { low: C.green, medium: C.amber, high: C.red }, SAFE = { normal: C.green, warning: C.amber, critical: C.red };
  const FP = {
    mount(el, opts) { const inst = { el, opts, key: null, polys: {}, mode: opts.mode || 'default', sel: opts.selected || null, data: null }; el.classList.add('fp'); return Object.assign(inst, FP.proto); },
    proto: {
      set(fp) {
        this.data = fp;
        if (!fp || !fp.floor) { this.el.innerHTML = '<div class="empty">No floor plan is mapped for this selection.</div>'; this.key = null; return; }
        const key = fp.floor.floor_id + '|' + fp.floor.image + '|' + fp.rooms.map(r => r.room_id).join(',');
        if (key !== this.key) this.build(fp, key); else this.paint();
      },
      build(fp, key) {
        const f = fp.floor; this.key = key;
        this.el.innerHTML = `<div class="mapbox"><img src="${esc(f.image)}" alt="Floor plan ${esc(f.name)} of ${esc(f.building)}" width="${f.width}" height="${f.height}"><svg viewBox="0 0 ${f.width} ${f.height}" role="group" aria-label="Room overlay"></svg><div class="tip"></div></div>${fp.unmapped_rooms.length ? `<div class="legend">${fp.unmapped_rooms.length} room(s) on this floor have no verified polygon and are not drawn.</div>` : ''}`;
        const svg = $('svg', this.el), tip = $('.tip', this.el), box = $('.mapbox', this.el); this.polys = {};
        fp.rooms.forEach(r => {
          const pts = r.polygon.map(p => `${(p[0] * f.width).toFixed(1)},${(p[1] * f.height).toFixed(1)}`).join(' ');
          const cx = r.polygon.reduce((a, p) => a + p[0], 0) / r.polygon.length * f.width, cy = r.polygon.reduce((a, p) => a + p[1], 0) / r.polygon.length * f.height;
          const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
          g.innerHTML = `<polygon class="room" tabindex="0" role="button" data-room="${r.room_id}" points="${pts}"></polygon>`;
          svg.appendChild(g);
          const poly = g.firstChild; this.polys[r.room_id] = poly;
          const show = e => { const rr = this.cur(r.room_id), b = box.getBoundingClientRect(); tip.innerHTML = FP.tipHtml(rr); tip.style.display = 'block'; const x = (e.clientX ? e.clientX - b.left : cx / f.width * b.width) + 12, y = e.clientY ? e.clientY - b.top : cy / f.height * b.height; tip.style.left = Math.min(b.width - 235, x) + 'px'; tip.style.top = Math.max(0, y - 10) + 'px'; };
          poly.addEventListener('mousemove', show); poly.addEventListener('focus', show); poly.addEventListener('mouseleave', () => tip.style.display = 'none'); poly.addEventListener('blur', () => tip.style.display = 'none');
          poly.addEventListener('click', () => this.pick(r.room_id)); poly.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.pick(r.room_id); } });
        });
        this.paint();
      },
      cur(id) { return this.data.rooms.find(r => r.room_id === id); },
      pick(id) { this.sel = this.sel === id ? null : id; this.paint(); if (this.opts.onSelect) this.opts.onSelect(this.sel ? this.cur(this.sel) : null); },
      select(id) { this.sel = id; if (this.data && this.data.floor) this.paint(); },
      setMode(m) { this.mode = m; if (this.data && this.data.floor) this.paint(); },
      paint() {
        this.data.rooms.forEach(r => {
          const p = this.polys[r.room_id]; if (!p) return;
          let fill = '#64748B', op = .3, dash = '';
          if (this.mode === 'occupancy') { if (HEAT[r.heat]) { fill = HEAT[r.heat]; op = .55; } else if (r.heat === 'unknown_capacity') { fill = C.cyan; op = .25; dash = '4 3'; } }
          else if (this.mode === 'safety') { if (SAFE[r.safety]) { fill = SAFE[r.safety]; op = .5; } else if (r.safety === 'stale') { fill = C.amber; op = .15; dash = '4 3'; } }
          else { fill = C.blue; op = .22; }
          p.style.fill = fill; p.style.fillOpacity = op; p.style.stroke = fill; p.setAttribute('stroke-dasharray', dash);
          p.classList.toggle('sel', this.sel === r.room_id);
          p.setAttribute('aria-label', `${r.label} room ${r.room_number}, occupancy ${r.metrics.occupancy.value ?? 'no data'}, safety ${r.safety}`);
          p.setAttribute('aria-pressed', String(this.sel === r.room_id));
        });
      }
    },
    tipHtml(r) {
      const m = r.metrics, v = (k, u, d = 1) => m[k].value == null ? `<span style="color:#8CA0B8">${SRC[m[k].state] || 'n/a'}</span>` : `${num(m[k].value, d)} ${u}`;
      return `<b>${esc(r.room_number)} · ${esc(r.label)}</b><br>Occupancy: ${v('occupancy', '', 0)}${r.capacity ? ' / ' + r.capacity + (r.capacity_source === 'demo' && S.labels ? ' (demo cap.)' : '') : ' (capacity not set)'}${r.utilization != null ? ' · ' + r.utilization + '%' : ''}<br>Temp ${v('temperature', '°C')} · CO₂ ${v('co2', 'ppm', 0)}<br>Safety: ${esc(r.safety)} · Devices ${r.devices_online}/${r.devices_total}${r.active_alarms ? ' · ' + r.active_alarms + ' alarm(s)' : ''}`;
    }
  };
  function planLegend(inst) {
    const L = { occupancy: [['Low', C.green], ['Medium', C.amber], ['High', C.red], ['Capacity unknown', C.cyan], ['No data', C.grey]], safety: [['Normal', C.green], ['Warning', C.amber], ['Critical', C.red], ['Stale / unavailable', C.grey]], default: [['Mapped room', C.blue]] };
    return `<div class="legend">${(L[inst.mode] || L.default).map(([t, c]) => `<span><i style="background:${c}"></i>${t}</span>`).join('')}</div>`;
  }

  /* ---------------- campus map ---------------- */
  function campusMap(el, c, onPick, selected) {
    if (!c || !c.image) { el.innerHTML = '<div class="empty">No campus map configured for this account.</div>'; el._built = false; return; }
    if (!el._built || el._img !== c.image) {
      el._built = true; el._img = c.image;
      el.innerHTML = `<div class="mapbox"><img src="${esc(c.image)}" alt="University campus map" width="${c.width}" height="${c.height}"><div class="tip"></div></div><div class="legend" id="cmleg"></div>`;
    }
    const box = $('.mapbox', el), tip = $('.tip', el), keep = new Set(c.buildings.map(b => 'm' + b.building_id));
    box.querySelectorAll('.marker').forEach(m => { if (!keep.has(m.id)) m.remove(); });
    c.buildings.forEach(b => {
      let m = box.querySelector('#m' + b.building_id);
      if (!m) { m = document.createElement('button'); m.id = 'm' + b.building_id; m.type = 'button'; box.appendChild(m); m.addEventListener('click', () => onPick(m._b)); }
      m._b = b; m.style.left = b.x * 100 + '%'; m.style.top = b.y * 100 + '%';
      m.className = 'marker' + (b.confirmed ? '' : ' unconf') + (b.active_alarms ? ' warning' : '') + (selected === b.building_id ? ' sel' : '');
      m.innerHTML = esc(b.code || b.name[0]) + (b.active_alarms ? `<span class="n">${b.active_alarms}</span>` : '');
      m.setAttribute('aria-label', `${b.name}: ${b.devices_online} sensors online, ${b.devices_offline} offline, ${b.active_alarms} active alarms`);
      m.onmouseenter = m.onfocus = () => { const bx = box.getBoundingClientRect(); tip.innerHTML = `<b>${esc(b.name)}</b>${b.confirmed ? '' : ' <span style="color:#F5B301">(mapping unconfirmed)</span>'}<br>${b.floors} floor(s) · ${b.rooms} room(s)<br>Sensors ${b.devices_online} on / ${b.devices_offline} off<br>Energy 24 h: ${b.energy_kwh_24h == null ? 'n/a' : num(b.energy_kwh_24h) + ' kWh'}<br>${b.temperature == null ? 'Temp n/a' : num(b.temperature) + ' °C'} · ${b.co2 == null ? 'CO₂ n/a' : num(b.co2, 0) + ' ppm'}<br>Alarms: ${b.active_alarms}<br>Updated ${ago(b.last_update)} · ${esc(b.source)}`; tip.style.display = 'block'; tip.style.left = Math.min(bx.width - 235, b.x * bx.width + 18) + 'px'; tip.style.top = Math.max(0, b.y * bx.height - 40) + 'px'; };
      m.onmouseleave = m.onblur = () => tip.style.display = 'none';
    });
    $('#cmleg', el).innerHTML = (c.unplaced.length ? `No campus marker yet (position not confirmed): ${c.unplaced.map(b => esc(b.name)).join(', ')} — place in Settings. ` : '') + (c.buildings.some(b => !b.confirmed) ? 'Dashed markers: letter read from the drawing, building identity not yet confirmed.' : '');
  }

  /* ---------------- shell ---------------- */
  const TITLES = { overview: '1. Smart Building Overview', energy: '2. Energy Dashboard', environment: '3. Environmental Dashboard', occupancy: '4. Occupancy Dashboard', safety: '5. Safety Dashboard', 'floor-plan': 'Floor Plan', reports: 'Reports', settings: 'Settings' };
  const REPORT = { overview: 1, energy: 1, environment: 1, occupancy: 1, safety: 1 };
  function shell(body) {
    app.innerHTML = `<div class="top"><button class="btn sec menu-btn" id="menu" aria-label="Menu">☰</button><h1>${TITLES[PAGE]}</h1><span class="sp"></span>
      <span class="pill" id="clock" title="Configured timezone"></span><span class="pill" id="status" role="status" aria-live="polite"><i class="dot"></i><span>Connecting…</span></span><span class="pill" id="srcpill"></span></div>
      <div id="banner"></div><div id="filters" class="filters"></div><div id="msg" aria-live="polite"></div><div id="view">${body}</div>`;
    $('#menu').onclick = () => $('#side').classList.toggle('open');
    const tick = () => { const d = new Date(); $('#clock').textContent = tzTime(d, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }) + ' ' + tzTime(d, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }); };
    tick(); clearInterval(S.clock); S.clock = setInterval(tick, 1000);
  }
  const msg = (t, k = 'err') => { $('#msg').innerHTML = t ? `<div class="msg ${k}" role="alert">${esc(t)}</div>` : ''; };
  function syncNav() { document.querySelectorAll('[data-nav]').forEach(a => { const keep = qs({}, ['severity', 'status']); a.href = '/dashboards/' + a.dataset.nav + (keep ? '?' + keep : ''); }); }
  function pushUrl() { history.replaceState(null, '', location.pathname + '?' + qs()); syncNav(); }

  function renderFilters() {
    const m = S.meta, f = S.f, el = $('#filters');
    const blds = [...new Map(m.floors.map(x => [x.building_id, x.building])).entries()];
    const floors = m.floors.filter(x => !f.building_id || x.building_id == f.building_id);
    const fl = new Set(floors.map(x => x.floor_id));
    const rooms = m.rooms.filter(r => f.floor_id ? r.floor_id == f.floor_id : fl.has(r.floor_id));
    const opt = (v, t, sel) => `<option value="${esc(v)}" ${String(sel) === String(v) ? 'selected' : ''}>${esc(t)}</option>`;
    if (['reports', 'settings'].includes(PAGE)) { el.innerHTML = ''; return; }
    el.innerHTML = `
      <label class="f">Building<select id="fb"><option value="">All buildings</option>${blds.map(([i, n]) => opt(i, n, f.building_id)).join('')}</select></label>
      <label class="f">Floor<select id="ff"><option value="">All floors</option>${floors.map(x => opt(x.floor_id, `${x.building} – ${x.floor}`, f.floor_id)).join('')}</select></label>
      <label class="f">Room<select id="fr"><option value="">All rooms</option>${rooms.map(r => opt(r.room_id, r.room_number ? `${r.room_number} ${r.label}` : r.room_name, f.room_id)).join('')}</select></label>
      <label class="f">Period<select id="fp">${[['1h', 'Last hour'], ['24h', 'Last 24 hours'], ['7d', 'Last 7 days'], ['30d', 'Last 30 days'], ['custom', 'Custom…']].map(([v, t]) => opt(v, t, f.start ? 'custom' : f.range)).join('')}</select></label>
      <span id="custom" style="display:${f.start ? 'flex' : 'none'};gap:6px"><label class="f">From<input type="date" id="fs" value="${esc((f.start || '').slice(0, 10))}"></label><label class="f">To<input type="date" id="fe" value="${f.end ? esc(new Date(Date.parse(f.end) - 86400000).toISOString().slice(0, 10)) : ''}"></label><button class="btn sec sm" id="apply" style="align-self:end">Apply</button></span>
      <span class="sp" style="flex:1"></span>
      ${f.room_id ? '<button class="btn sec sm" id="clear">Clear room selection</button>' : ''}
      ${REPORT[PAGE] ? `<button class="btn" id="dl" data-type="${PAGE}">⬇ Download ${PAGE[0].toUpperCase() + PAGE.slice(1)} PDF Report</button>` : ''}`;
    const chg = () => { pushUrl(); Poll.now(); };
    $('#fb').onchange = e => { f.building_id = e.target.value || undefined; delete f.floor_id; delete f.room_id; renderFilters(); chg(); };
    $('#ff').onchange = e => { f.floor_id = e.target.value || undefined; delete f.room_id; if (f.floor_id) f.building_id = String(m.floors.find(x => x.floor_id == f.floor_id).building_id); renderFilters(); chg(); };
    $('#fr').onchange = e => { f.room_id = e.target.value || undefined; if (f.room_id) { const r = m.rooms.find(x => x.room_id == f.room_id); f.floor_id = String(r.floor_id); f.building_id = String(m.floors.find(x => x.floor_id == r.floor_id).building_id); } renderFilters(); chg(); if (S.plan) S.plan.select(f.room_id ? +f.room_id : null); };
    $('#fp').onchange = e => { if (e.target.value === 'custom') { $('#custom').style.display = 'flex'; return; } delete f.start; delete f.end; f.range = e.target.value; $('#custom').style.display = 'none'; chg(); };
    $('#apply').onclick = () => { const a = $('#fs').value, b = $('#fe').value; if (!a || !b) return msg('Choose both dates'); if (b < a) return msg('End date must not precede the start date'); msg(''); f.start = a + 'T00:00:00+00:00'; f.end = new Date(Date.parse(b + 'T00:00:00Z') + 86400000).toISOString().slice(0, 19) + '+00:00'; chg(); };
    const cl = $('#clear'); if (cl) cl.onclick = () => { delete f.room_id; renderFilters(); chg(); if (S.plan) S.plan.select(null); };
    const dl = $('#dl'); if (dl) dl.onclick = () => downloadReport(dl.dataset.type, S.f, dl);
  }
  /* Picking a room on any plan scopes the whole page to that room; picking it again clears it. */
  function onRoomPick(r) {
    if (r) { const fl = S.data.floor_plan.floor; S.f.room_id = String(r.room_id); S.f.floor_id = String(fl.floor_id); S.f.building_id = String(fl.building_id); } else delete S.f.room_id;
    renderFilters(); pushUrl(); Poll.now();
  }

  async function downloadReport(type, f, btn) {
    const old = btn.textContent; btn.disabled = true; btn.textContent = 'Generating PDF…'; msg('');
    try {
      const p = new URLSearchParams(); ['building_id', 'floor_id', 'room_id', 'range', 'start', 'end'].forEach(k => f[k] && p.set(k, f[k]));
      const r = await fetch(`/api/reports/${type}/pdf?${p}`, { credentials: 'same-origin' });
      if (!r.ok) { let d = 'The report could not be generated'; try { d = (await r.json()).detail || d; } catch (e) { } throw new Error(d); }
      if (!(r.headers.get('content-type') || '').includes('application/pdf')) throw new Error('The server did not return a PDF');
      const blob = await r.blob();
      const name = ((r.headers.get('content-disposition') || '').match(/filename="([^"]+)"/) || [])[1] || `smart_building_${type}_report.pdf`;
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      msg(`Downloaded ${name}`, 'info');
    } catch (e) { msg('Report failed: ' + e.message, 'err'); } finally { btn.disabled = false; btn.textContent = old; }
  }

  function setStatus(d) {
    const st = $('#status'), sp = $('#srcpill');
    if (S.failing) { st.className = 'pill s-off'; st.lastChild.textContent = `Disconnected – showing last data (${tzTime(S.lastOk, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })})`; return; }
    if (!d) return;
    const m = d.meta, stale = m.last_update && (Date.now() - new Date(m.last_update)) / 1000 > m.freshness_seconds;
    S.labels = m.labels !== false;
    st.className = 'pill ' + (!m.device_count || stale ? 's-stale' : (m.source === 'live' || !S.labels) ? 's-live' : 's-simulated');
    st.lastChild.textContent = !m.device_count ? 'NO DEVICES IN SCOPE' : stale ? `STALE – last reading ${ago(m.last_update)}` : (!S.labels ? 'Online' : m.source === 'live' ? 'LIVE' : m.source === 'simulated' ? 'LIVE · SIMULATED' : 'LIVE · MIXED') + ` · updated ${ago(m.last_update)}`;
    sp.textContent = S.labels ? 'Source: ' + (SRC[m.source] || m.source) : ''; sp.style.display = S.labels ? '' : 'none';
    $('#banner').innerHTML = S.labels && (m.source === 'simulated' || m.source === 'mixed') ? '<div class="banner">Demonstration data: values marked SIMULATED are generated by the Python simulator, not read from physical sensors.</div>' : '';
  }

  /* ---------------- polling: one loop per page ---------------- */
  const Poll = {
    t: null, busy: false, ctl: null,
    async run() {
      if (this.busy) return;
      if (document.hidden) return this.sched();
      this.busy = true; this.ctl = new AbortController();
      try {
        const d = await api(`/api/dashboards/${PAGE}?${qs()}`, { signal: this.ctl.signal });
        S.data = d; S.lastOk = new Date(); if (S.failing) msg(''); S.failing = false; setStatus(d); PAGES[PAGE].update(d);
      } catch (e) { if (e.name !== 'AbortError') { S.failing = true; if (S.lastOk) setStatus(); else if (e.message !== 'auth') { msg(e.message); setStatus(); } } }
      finally { this.busy = false; if (!this.ctl.signal.aborted) this.sched(); }
    },
    sched() { clearTimeout(this.t); this.t = setTimeout(() => this.run(), POLL); },
    now() { if (this.ctl) this.ctl.abort(); this.busy = false; clearTimeout(this.t); this.run(); }
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden && PAGES[PAGE].live) Poll.now(); });
  window.addEventListener('beforeunload', () => { clearTimeout(Poll.t); clearInterval(S.clock); });

  const series = (name, data, color, o = {}) => ({ name, data, color, ...o });
  const panel = (title, id) => `<div class="panel"><h3>${title}</h3><div class="chart" id="${id}"></div></div>`;
  const PAGES = {};

  /* Human-readable alarm reading: 'Detected' for switch-type sensors, value + unit vs limit otherwise. */
  const AL_UNIT = { co2: 'ppm', temperature: '°C', humidity: '%', pm25: 'µg/m³', pm10: 'µg/m³', water_lpm: 'L/min', power_w: 'W' };
  const AL_NAME = { co2: 'CO₂', temperature: 'Temperature', humidity: 'Humidity', pm25: 'PM2.5', smoke: 'Smoke sensor', leak: 'Leak sensor', door_open: 'Door contact' };
  const binaryAlarm = a => a.operator === '==' || ['smoke', 'leak', 'door_open', 'gas'].includes(a.field);
  const reading = a => binaryAlarm(a) ? (a.field === 'door_open' ? 'Open' : 'Detected') : a.value == null ? '–' : `${num(+a.value, 1)} ${AL_UNIT[a.field] || ''}`.trim();
  const limitTxt = a => binaryAlarm(a) || a.threshold == null ? '' : `${a.operator === '<' ? 'min.' : 'limit'} ${num(+a.threshold, 1)} ${AL_UNIT[a.field] || ''}`.trim();
  function alarmTable(al, clickable) {
    if (!al.length) return '<div class="empty">No alarms in this selection.</div>';
    return `<div class="scroll"><table class="t"><thead><tr><th>Time</th><th>Device</th><th>Event</th><th>Severity</th><th>Location</th><th>Value / limit</th><th>Status</th></tr></thead><tbody>${al.map(a => `<tr class="${clickable ? 'click' : ''}" data-id="${a.id}"><td style="white-space:nowrap">${esc(when(a.time))}</td><td>${esc(a.device || a.device_id)}</td><td>${esc(a.event || a.type)}${a.simulated ? ' ' + badge('simulated') : ''}</td><td>${badge(a.severity === 'critical' ? 'critical' : a.severity === 'warning' ? 'warning' : 'normal', a.severity)}</td><td>${esc([a.building, a.floor, a.room].filter(Boolean).join(' / '))}${a.mapped ? '' : ' <span style="color:var(--muted)">(not on plan)</span>'}</td><td style="white-space:nowrap">${esc(reading(a))}${limitTxt(a) ? `<br><span style="color:var(--muted);font-size:10.5px">${esc(limitTxt(a))}</span>` : ''}</td><td>${badge(a.status === 'active' ? 'critical' : a.status === 'acknowledged' ? 'warning' : 'normal', a.status)}</td></tr>`).join('')}</tbody></table></div>`;
  }

  PAGES.overview = {
    live: true,
    init() {
      shell(`<div class="grid kpis k4" id="kpis"></div>
        <div class="grid split" style="margin-top:10px"><div class="panel"><h3>Campus map <span class="legend" style="margin:0">Click a building marker</span></h3><div id="campus"></div><div id="bsel" class="detail" style="margin-top:8px"></div></div>
          <div class="panel"><h3>Building status</h3><div id="systems"></div></div></div>
        <div class="grid two" style="margin-top:10px">${panel('Temperature & humidity', 'c-th')}${panel('Electrical power (W)', 'c-pow')}${panel('CO₂ & PM2.5', 'c-air')}${panel('Occupancy', 'c-occ')}</div>
        <div class="grid two" style="margin-top:10px"><div class="panel"><h3>Energy by room (kWh)</h3><div id="c-room"></div></div><div class="panel"><h3>Floor plan <select id="pm" aria-label="Overlay"><option value="occupancy">Occupancy heatmap</option><option value="safety">Safety state</option></select></h3><div id="fp"></div><div id="fpl"></div></div></div>
        <div class="panel" style="margin-top:10px"><h3>Recent alarms</h3><div id="al"></div></div>`);
      S.plan = FP.mount($('#fp'), { mode: 'occupancy', onSelect: onRoomPick, selected: S.f.room_id ? +S.f.room_id : null });
      $('#pm').onchange = e => { S.plan.setMode(e.target.value); $('#fpl').innerHTML = planLegend(S.plan); };
    },
    update(d) {
      const k = d.kpis, sf = d.safety, pw = k.power_w, pc = pw.comparison;
      $('#kpis').innerHTML = [
        kpiCard('Temperature', k.temperature, { icon: 'temp', color: C.red }), kpiCard('Humidity', k.humidity, { icon: 'hum', d: 0 }),
        kpiCard('CO₂ level', k.co2, { icon: 'co2', color: '#cbd5e1', d: 0 }), kpiCard('Occupancy (est.)', k.occupancy, { icon: 'occ', color: C.blue, d: 0 }),
        kpiCard('Current power', { ...pw, value: pw.value == null ? null : pw.value / 1000, unit: 'kW', comparison: pc && pc.available ? { ...pc, diff: pc.diff / 1000, unit: 'kW' } : pc }, { icon: 'pow', color: C.amber, d: 2 }),
        kpiCard('Water flow', k.water_lpm, { icon: 'water' }), kpiCard('Light intensity', k.lux, { icon: 'lux', color: C.amber, d: 0 }),
        statCard('Safety status', sf.state, sf.state === 'unavailable' ? 'No safety sensors' : sf.state[0].toUpperCase() + sf.state.slice(1), 'shield', C.green)].join('');
      campusMap($('#campus'), d.campus, b => { S.f.building_id = String(b.building_id); delete S.f.floor_id; delete S.f.room_id; renderFilters(); pushUrl(); Poll.now(); }, +S.f.building_id || null);
      const sel = d.campus && d.campus.buildings.find(b => b.building_id == S.f.building_id);
      $('#bsel').innerHTML = sel ? `<b>${esc(sel.name)}</b> selected — panels below are scoped to this building. <a href="/dashboards/floor-plan?${qs()}" style="color:var(--cyan)">Open floor plan →</a> <button class="btn sec sm" id="allb">Show all buildings</button>` : '';
      const ab = $('#allb'); if (ab) ab.onclick = () => { delete S.f.building_id; renderFilters(); pushUrl(); Poll.now(); };
      $('#systems').innerHTML = d.systems.map(s => `<div class="sysrow"><span>${esc(s.name)}${s.note ? `<br><span style="color:var(--muted);font-size:10.5px">${esc(s.note)}</span>` : ''}</span>${badge(s.state, s.state === 'normal' ? 'Normal' : null)}</div>`).join('');
      const t = d.trends;
      SBChart.line($('#c-th'), { label: 'temperature and humidity', series: [series('Temperature (°C)', t.temperature, C.red, { unit: '°C', fill: true }), series('Humidity (%)', t.humidity, C.blue, { unit: '%', axis: 'r' })] });
      SBChart.line($('#c-pow'), { label: 'power', series: [series('Power', t.power_w, C.amber, { unit: 'W', fill: true })] });
      SBChart.line($('#c-air'), { label: 'co2 and pm2.5', series: [series('CO₂ (ppm)', t.co2, C.cyan, { unit: 'ppm' }), series('PM2.5 (µg/m³)', t.pm25, C.red, { unit: 'µg/m³', axis: 'r' })] });
      SBChart.line($('#c-occ'), { label: 'occupancy', series: [series('Occupancy', t.occupancy, C.green, { unit: 'people', fill: true })] });
      SBChart.hbars($('#c-room'), { items: Object.entries(d.by_room_kwh).map(([l, v]) => ({ label: l, value: v, text: v + ' kWh' })) });
      S.plan.set(d.floor_plan); $('#fpl').innerHTML = planLegend(S.plan);
      $('#al').innerHTML = alarmTable(d.alarms, false);
    }
  };

  PAGES.energy = {
    live: true,
    init() {
      shell(`<div class="grid kpis" id="kpis"></div><div class="grid two" style="margin-top:10px">${panel('Power consumption (W)', 'c-pow')}<div class="panel"><h3>Energy consumption by area (kWh)</h3><div id="c-dist"></div></div>${panel('Daily energy consumption (kWh)', 'c-day')}<div class="panel"><h3>Energy by room (kWh)</h3><div id="c-room"></div></div></div>
      <div class="grid two" style="margin-top:10px"><div class="panel"><h3>Demand statistics</h3><div id="stats"></div></div><div class="panel"><h3>Calculation notes</h3><div class="detail" id="notes"></div></div></div>`);
    },
    update(d) {
      const k = d.kpis, cur = d.tariff.currency, cost = k.cost, pk = k.peak_power, pcmp = pk.comparison;
      $('#kpis').innerHTML = [
        kpiCard('Energy consumed', k.energy_kwh, { icon: 'pow', color: C.amber }),
        cost.value == null ? statCard('Estimated cost', 'not_configured', 'Unavailable', 'cost', C.amber, cost.unavailable_reason) : kpiCard('Estimated cost', { value: cost.value, unit: cur, state: k.energy_kwh.state, comparison: cost.comparison }, { icon: 'cost', color: C.amber, d: 2 }),
        kpiCard('Peak power', { value: pk.value == null ? null : pk.value / 1000, unit: 'kW', state: pk.value == null ? 'no_data' : k.energy_kwh.state, comparison: pcmp.available ? { ...pcmp, diff: pcmp.diff / 1000, unit: 'kW' } : pcmp }, { icon: 'gauge', color: C.red, d: 2 }),
        k.renewable.value == null ? statCard('Renewable usage', k.renewable.state, k.renewable.state === 'not_configured' ? 'Not configured' : 'No data', 'leaf', C.green, k.renewable.note) : kpiCard('Renewable usage', k.renewable, { icon: 'leaf', color: C.green, d: 0 })].join('');
      SBChart.line($('#c-pow'), { label: 'power', series: [series('Power', d.power_series, C.blue, { unit: 'W', fill: true })] });
      SBChart.donut($('#c-dist'), { items: d.distribution.map(([l, v]) => ({ label: l, value: v })), unit: 'kWh' });
      SBChart.bars($('#c-day'), { items: Object.entries(d.daily_kwh).slice(-14).map(([l, v]) => ({ label: l.slice(5), value: +v.toFixed(1) })), unit: ' kWh' });
      SBChart.hbars($('#c-room'), { items: d.by_room.map(([l, v]) => ({ label: l, value: v, text: v + ' kWh' })) });
      const s = d.power_stats;
      $('#stats').innerHTML = `<table class="t"><tbody><tr><td>Average demand</td><td>${num(s.avg, 0)} W</td></tr><tr><td>Minimum demand</td><td>${num(s.min, 0)} W</td></tr><tr><td>Maximum demand</td><td>${num(s.max, 0)} W</td></tr><tr><td>Samples</td><td>${s.n}</td></tr><tr><td>Abnormal-demand samples (&gt; mean + 2 sd)</td><td>${d.abnormal.length}</td></tr></tbody></table>`;
      $('#notes').innerHTML = `<dt>Energy vs power</dt><dd>Energy (kWh) is the meter difference between successive readings (resets ignored) or, without a meter, hourly average power × time. Hours with no readings are unknown, not zero.</dd><dt>Method used</dt><dd>${esc(k.energy_kwh.method.join(', ') || 'no energy data')}</dd><dt>Tariff</dt><dd>${d.tariff.configured ? esc(d.tariff.type) + ' · ' + esc(cur) : 'No tariff configured – cost is not calculated. Set it in Settings.'}</dd>`;
    }
  };

  PAGES.environment = {
    live: true,
    init() {
      shell(`<div class="grid kpis" id="kpis"></div><div class="grid two" style="margin-top:10px">${panel('Temperature & humidity trend', 'c-th')}${panel('CO₂ levels by room (latest)', 'c-co2')}${panel('Light intensity (lux)', 'c-lux')}<div class="panel"><h3>Indoor comfort index</h3><div id="gauge" class="chart" style="text-align:center"></div><div class="detail" id="cn"></div></div></div>
      <div class="grid two" style="margin-top:10px">${panel('PM2.5 (µg/m³)', 'c-pm')}<div class="panel"><h3>Statistics for the period</h3><div id="stats"></div></div></div>`);
    },
    update(d) {
      const k = d.kpis, sc = d.comfort.score;
      $('#kpis').innerHTML = [kpiCard('Temperature', k.temperature, { icon: 'temp', color: C.red }), kpiCard('Humidity', k.humidity, { icon: 'hum', d: 0 }), kpiCard('CO₂ level', k.co2, { icon: 'co2', color: '#cbd5e1', d: 0 }), kpiCard('PM2.5', k.pm25, { icon: 'pm' }), kpiCard('Light intensity', k.lux, { icon: 'lux', color: C.amber, d: 0 }),
        statCard('Air quality score', sc == null ? 'no_data' : sc >= 80 ? 'normal' : sc >= 60 ? 'warning' : 'critical', sc == null ? 'n/a' : sc + ' / 100', 'gauge', C.green)].join('');
      const s = d.series, th = d.thresholds.co2;
      SBChart.line($('#c-th'), { label: 'temperature and humidity', series: [series('Temperature (°C)', s.temperature, C.red, { unit: '°C', fill: true }), series('Humidity (%)', s.humidity, C.blue, { unit: '%', axis: 'r' })] });
      SBChart.bars($('#c-co2'), { items: d.co2_by_room.map(r => ({ label: r.room.replace(/^\S+\s/, ''), value: r.value == null ? null : Math.round(r.value), color: r.status === 'critical' ? C.red : r.status === 'warning' ? C.amber : C.green })), unit: ' ppm', threshold: th.warn ? { value: th.warn, label: `Warning limit ${th.warn} ppm` } : null });
      SBChart.line($('#c-lux'), { label: 'light', series: [series('Light (lux)', s.lux, C.amber, { unit: 'lux', fill: true })] });
      SBChart.line($('#c-pm'), { label: 'pm2.5', series: [series('PM2.5', s.pm25, C.cyan, { unit: 'µg/m³', fill: true })], thresholds: d.thresholds.pm25.warn ? [{ value: d.thresholds.pm25.warn, label: 'warning limit', color: C.amber }] : [] });
      SBChart.gauge($('#gauge'), { value: sc });
      $('#cn').innerHTML = `<dt>Method (thresholds configurable)</dt><dd>${esc(d.comfort.method)}</dd>${d.minutes_co2_above_warning != null ? `<dt>CO₂ above warning limit</dt><dd>${d.minutes_co2_above_warning} min in this period</dd>` : ''}`;
      $('#stats').innerHTML = `<table class="t"><thead><tr><th>Metric</th><th>Min</th><th>Avg</th><th>Max</th></tr></thead><tbody>${Object.entries(d.stats).map(([m, v]) => `<tr><td>${m}</td><td>${num(v.min)}</td><td>${num(v.avg)}</td><td>${num(v.max)}</td></tr>`).join('')}</tbody></table>`;
    }
  };

  PAGES.occupancy = {
    live: true,
    init() {
      shell(`<div class="grid kpis" id="kpis"></div><div class="grid two" style="margin-top:10px">${panel('Occupancy trend', 'c-occ')}<div class="panel"><h3>Occupancy by room</h3><div id="c-room"></div></div>
      <div class="panel"><h3>Occupancy heatmap — floor plan</h3><div id="fp"></div><div id="fpl"></div></div>${panel('Occupancy vs energy', 'c-sc')}</div>
      <div class="panel" style="margin-top:10px"><h3>Hourly profile (average occupancy by hour of day)</h3><div class="chart" id="c-prof"></div></div>`);
      S.plan = FP.mount($('#fp'), { mode: 'occupancy', onSelect: onRoomPick, selected: S.f.room_id ? +S.f.room_id : null });
    },
    update(d) {
      const k = d.kpis;
      $('#kpis').innerHTML = [kpiCard('Current occupancy (estimate)', k.current, { icon: 'occ', color: C.blue, d: 0 }),
        k.utilization.value == null ? statCard('Building utilisation', 'capacity_not_configured', 'Needs capacity', 'gauge', C.green) : kpiCard('Building utilisation', { value: k.utilization.value, unit: '%', state: k.current.state }, { icon: 'gauge', color: C.green }),
        statCard('Peak hours (observed)', k.peak_hours.value ? 'normal' : 'no_data', k.peak_hours.value || 'Not enough history', 'clock', C.cyan), statCard('Rooms in scope', 'normal', String(k.rooms.value), 'rooms', C.blue)].join('');
      SBChart.line($('#c-occ'), { label: 'occupancy', series: [series('Occupancy', d.series, C.green, { unit: 'people', fill: true })] });
      SBChart.hbars($('#c-room'), { items: d.by_room.map(r => ({ label: r.room, value: r.occupancy, max: r.capacity || undefined, cls: r.utilization == null ? '' : r.utilization >= 80 ? 'high' : r.utilization >= 50 ? 'medium' : 'ok', text: r.occupancy == null ? (SRC[r.state] || 'n/a') : `${r.occupancy}${r.capacity ? '/' + r.capacity : ''}${r.utilization != null ? ' ' + Math.round(r.utilization) + '%' : ''}` })) });
      S.plan.set(d.floor_plan); $('#fpl').innerHTML = planLegend(S.plan) + `<div class="legend">Utilisation = occupancy ÷ room capacity.${S.labels ? ' Capacities marked "demo" are placeholders for the simulation.' : ''}</div>`;
      SBChart.scatter($('#c-sc'), { points: d.scatter.points, regression: d.scatter.regression, xLabel: 'Occupancy (people)', yLabel: 'Power (kW)' });
      SBChart.bars($('#c-prof'), { items: Object.entries(d.hourly_profile).map(([h, v]) => ({ label: String(h).padStart(2, '0'), value: v })), unit: ' people', empty: 'Hourly profile needs a longer period' });
    }
  };

  PAGES.safety = {
    live: true,
    init() {
      shell(`<div class="grid safecards" id="cards"></div><div class="grid split" style="margin-top:10px"><div class="panel"><h3>Recent alerts & alarm history
        <span style="display:flex;gap:6px"><select id="sv" aria-label="Severity"><option value="">All severities</option><option>critical</option><option>warning</option><option>info</option></select><select id="ss" aria-label="Status"><option value="">All states</option><option>active</option><option>resolved</option></select></span></h3><div id="al"></div></div>
        <div class="panel"><h3>Alarm detail</h3><div id="ad" class="detail"><div class="empty">Select an alarm</div></div></div></div>
        <div class="panel" style="margin-top:10px"><h3>Safety status map</h3><div id="fp"></div><div id="fpl"></div></div>`);
      S.plan = FP.mount($('#fp'), { mode: 'safety', onSelect: onRoomPick, selected: S.f.room_id ? +S.f.room_id : null });
      $('#sv').value = S.f.severity || ''; $('#ss').value = S.f.status || '';
      $('#sv').onchange = e => { S.f.severity = e.target.value || undefined; pushUrl(); Poll.now(); }; $('#ss').onchange = e => { S.f.status = e.target.value || undefined; pushUrl(); Poll.now(); };
      $('#al').addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (!tr) return; S.selAlarm = +tr.dataset.id; this.detail(); const a = S.data.alarms.find(x => x.id === S.selAlarm); if (a && a.room_id && a.mapped) S.plan.select(a.room_id); document.querySelectorAll('#al tr.sel').forEach(r => r.classList.remove('sel')); tr.classList.add('sel'); });
    },
    detail() {
      const a = (S.data.alarms || []).find(x => x.id === S.selAlarm), el = $('#ad');
      if (!a) { el.innerHTML = '<div class="empty">Select an alarm</div>'; return; }
      el.innerHTML = `<b>${esc(a.event || a.type)}</b> ${a.simulated ? badge('simulated') : ''}<dl><dt>Status</dt><dd>${badge(a.status === 'active' ? 'critical' : a.status === 'acknowledged' ? 'warning' : 'normal', a.status)}${a.acknowledged && !a.resolved ? ' <span style="color:var(--muted)">Acknowledged ≠ resolved</span>' : ''}</dd><dt>Triggered</dt><dd>${esc(when(a.time, { dateStyle: 'medium', timeStyle: 'medium' }))}</dd><dt>Device</dt><dd>${esc(a.device || a.device_id)}</dd><dt>Location</dt><dd>${esc([a.building, a.floor, a.room].filter(Boolean).join(' / '))}${a.mapped ? '' : ' (room has no verified polygon)'}</dd><dt>Reading</dt><dd>${esc((AL_NAME[a.field] || a.field || '') + ': ' + reading(a))}${limitTxt(a) ? ` <span style="color:var(--muted)">(${a.severity} ${esc(limitTxt(a))})</span>` : ''}</dd>${a.resolved_at ? `<dt>Resolved</dt><dd>${esc(when(a.resolved_at, { dateStyle: 'medium', timeStyle: 'medium' }))}</dd>` : ''}</dl>
      ${S.data.can_manage && !a.resolved ? `<div style="display:flex;gap:6px;margin-top:8px">${a.acknowledged ? '' : '<button class="btn sec sm" data-act="acknowledge">Acknowledge</button>'}<button class="btn sm" data-act="resolve">Resolve</button></div>` : ''}`;
      el.querySelectorAll('[data-act]').forEach(b => b.onclick = async () => { try { b.disabled = true; await send('POST', `/alarms/${a.id}/${b.dataset.act}`, {}); msg(`Alarm ${b.dataset.act}d`, 'info'); Poll.now(); } catch (e) { msg(e.message); } });
    },
    update(d) {
      $('#cards').innerHTML = d.cards.map(c => {
        const icn = { smoke: 'flame', leak: 'water', gas: 'gas', door_open: 'door', devices: 'dev' }[c.key];
        const txt = c.state === 'normal' ? (c.key === 'door_open' ? 'All closed' : c.key === 'devices' ? 'All reporting' : 'Normal') : c.state === 'critical' ? 'ALARM' : c.state === 'warning' ? (c.key === 'door_open' ? 'Open' : c.value + ' offline') : (SRC[c.state] || c.state);
        return statCard(c.label, c.state, txt, icn, c.state === 'normal' ? C.green : c.state === 'critical' ? C.red : C.amber);
      }).join('');
      $('#al').innerHTML = alarmTable(d.alarms, true);
      const row = S.selAlarm && $(`#al tr[data-id="${S.selAlarm}"]`); if (row) row.classList.add('sel');
      this.detail(); S.plan.set(d.floor_plan); $('#fpl').innerHTML = planLegend(S.plan) + '<div class="legend">A room without a safety sensor is shown grey, never green.</div>';
    }
  };

  PAGES['floor-plan'] = {
    live: true,
    init() {
      shell(`<div class="grid split"><div class="panel"><h3>Floor plan<select id="pm" aria-label="Overlay"><option value="default">Rooms</option><option value="occupancy">Occupancy heatmap</option><option value="safety">Safety state</option></select></h3><div id="fp"></div><div id="fpl"></div></div>
      <div class="panel"><h3>Room details <button class="btn sec sm" id="back" style="display:none">← Whole floor</button></h3><div id="rd" class="detail"><div class="empty">Click a room on the plan</div></div><div class="chart" id="rt" style="margin-top:8px"></div></div></div>`);
      S.plan = FP.mount($('#fp'), { mode: 'default', onSelect: () => { onRoomPick(S.plan.sel ? S.plan.cur(S.plan.sel) : null); this.room(); }, selected: S.f.room_id ? +S.f.room_id : null });
      $('#pm').onchange = e => { S.plan.setMode(e.target.value); $('#fpl').innerHTML = planLegend(S.plan); };
      $('#back').onclick = () => { delete S.f.room_id; S.plan.select(null); renderFilters(); pushUrl(); this.room(); };
    },
    async room() {
      const id = S.f.room_id, back = $('#back');
      back.style.display = id ? '' : 'none';
      if (!id) { $('#rd').innerHTML = '<div class="empty">Click a room on the plan</div>'; $('#rt').innerHTML = ''; $('#rt')._sig = null; return; }
      try {
        const t = await api(`/api/rooms/${id}/telemetry?${qs({}, ['room_id'])}`);
        if (S.f.room_id != id) return;
        const r = S.data.floor_plan.rooms.find(x => x.room_id == id), c = t.current, v = (k, u, d = 1) => c[k].value == null ? `<span style="color:var(--muted)">${SRC[c[k].state]}</span>` : `${num(c[k].value, d)} ${u} ${c[k].state === 'simulated' ? badge('simulated') : ''}`;
        $('#rd').innerHTML = `<b style="font-size:14px">${esc(r ? r.room_number + ' · ' + r.label : 'Room ' + id)}</b><dl><dt>Occupancy</dt><dd>${v('occupancy', 'people', 0)}${r && r.capacity ? ' of ' + r.capacity + (r.capacity_source === 'demo' && S.labels ? ' (demo capacity)' : '') + (r.utilization != null ? ' · ' + r.utilization + '%' : '') : ' · capacity not configured'}</dd><dt>Temperature / humidity</dt><dd>${v('temperature', '°C')} · ${v('humidity', '%', 0)}</dd><dt>CO₂ / PM2.5</dt><dd>${v('co2', 'ppm', 0)} · ${v('pm25', 'µg/m³')}</dd><dt>Light</dt><dd>${v('lux', 'lux', 0)}</dd><dt>Power</dt><dd>${v('power_w', 'W', 0)}</dd><dt>Devices</dt><dd>${t.devices.map(d => `${esc(d.label || d.device_id)}${S.labels ? ' · ' + (d.source === 'live' ? 'LIVE' : 'SIMULATED') : ''} · updated ${ago(d.last_update)}`).join('<br>') || 'No device assigned'}</dd><dt>Active alarms</dt><dd>${t.alarms.filter(a => a.status === 'active').length || 'None'}</dd></dl>`;
        SBChart.line($('#rt'), { label: 'room trend', series: [series('Temperature (°C)', t.series.temperature, C.red, { unit: '°C' }), series('CO₂ (ppm)', t.series.co2, C.cyan, { unit: 'ppm', axis: 'r' })], height: 190, empty: 'No history for this room' });
      } catch (e) { $('#rd').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    },
    update(d) {
      S.plan.set(d.floor_plan); $('#fpl').innerHTML = planLegend(S.plan);
      const t = Date.now(); if (!this._t || t - this._t > 8000) { this._t = t; this.room(); }
    }
  };

  PAGES.reports = {
    live: false,
    init() {
      shell(`<div class="panel"><h2>Generate a PDF report</h2><div class="formgrid" id="rf"></div><div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap" id="rb"></div><p style="color:var(--muted);font-size:11px">Reports use the same backend calculations as the dashboards. Dates are interpreted as UTC. Leave dates empty to use the last 24 hours.</p></div>
      <div class="panel" style="margin-top:10px"><h3>Quick downloads (use the scope above)</h3><div style="display:flex;gap:8px;flex-wrap:wrap" id="qd"></div></div>`);
    },
    setup() {
      const m = S.meta, f = S.f, blds = [...new Map(m.floors.map(x => [x.building_id, x.building])).entries()];
      const opt = (v, t, s) => `<option value="${esc(v)}" ${String(s) === String(v) ? 'selected' : ''}>${esc(t)}</option>`;
      const render = () => {
        const floors = m.floors.filter(x => !f.building_id || x.building_id == f.building_id), rooms = m.rooms.filter(r => f.floor_id ? r.floor_id == f.floor_id : floors.some(x => x.floor_id === r.floor_id));
        const sd = f.start ? f.start.slice(0, 10) : '', ed = f.end ? new Date(Date.parse(f.end) - 86400000).toISOString().slice(0, 10) : '';
        $('#rf').innerHTML = `<label class="f">Report type<select id="rt">${Object.keys(REPORT).map(t => opt(t, t[0].toUpperCase() + t.slice(1), f.type || 'overview')).join('')}</select></label>
          <label class="f">Building<select id="rb_"><option value="">All</option>${blds.map(([i, n]) => opt(i, n, f.building_id)).join('')}</select></label>
          <label class="f">Floor<select id="rf_"><option value="">All</option>${floors.map(x => opt(x.floor_id, `${x.building} – ${x.floor}`, f.floor_id)).join('')}</select></label>
          <label class="f">Room<select id="rr_"><option value="">All</option>${rooms.map(r => opt(r.room_id, r.room_number ? `${r.room_number} ${r.label}` : r.room_name, f.room_id)).join('')}</select></label>
          <label class="f">Start date<input type="date" id="rs" value="${esc(sd)}"></label><label class="f">End date<input type="date" id="re" value="${esc(ed)}"></label>`;
        $('#rb').innerHTML = '<button class="btn" id="gen">Generate PDF</button>';
        $('#qd').innerHTML = Object.keys(REPORT).map(t => `<button class="btn sec qd" data-t="${t}">Download ${t[0].toUpperCase() + t.slice(1)} PDF Report</button>`).join('');
        $('#rt').onchange = () => { f.type = $('#rt').value; };
        $('#rb_').onchange = e => { f.building_id = e.target.value || undefined; delete f.floor_id; delete f.room_id; render(); };
        $('#rf_').onchange = e => { f.floor_id = e.target.value || undefined; delete f.room_id; render(); };
        $('#rr_').onchange = e => { f.room_id = e.target.value || undefined; };
        const scope = () => { const a = $('#rs').value, b = $('#re').value, o = { ...f }; delete o.start; delete o.end; if (a || b) { if (!a || !b) throw new Error('Choose both a start and an end date'); if (b < a) throw new Error('End date must not precede the start date'); o.start = a + 'T00:00:00+00:00'; o.end = new Date(Date.parse(b + 'T00:00:00Z') + 86400000).toISOString().slice(0, 19) + '+00:00'; } return o; };
        const go = (t, btn) => { try { downloadReport(t, scope(), btn); } catch (e) { msg(e.message); } };
        $('#gen').onclick = e => go($('#rt').value, e.target);
        document.querySelectorAll('.qd').forEach(b => b.onclick = () => go(b.dataset.t, b));
      };
      render();
    },
    update() { }
  };

  PAGES.settings = {
    live: false,
    init() { shell('<div id="sv"><div class="empty">Loading settings…</div></div>'); },
    async setup() {
      const m = S.meta, c = m.settings, dis = ADMIN ? '' : 'disabled', th = c.thresholds, tr = c.tariff;
      let campusData = null; try { campusData = await api('/api/campus/map'); } catch (e) { }
      const field = (id, label, val, type = 'number', extra = '') => `<label class="f">${label}<input id="${id}" type="${type}" value="${val ?? ''}" ${extra} ${dis}></label>`;
      $('#sv').innerHTML = `${ADMIN ? '' : '<div class="msg info">Settings are read-only for client accounts.</div>'}
      <div class="grid two"><div class="panel"><h3>Electricity tariff</h3><div class="formgrid">${field('t_cur', 'Currency (ISO)', tr.currency, 'text', 'maxlength=3')}<label class="f">Tariff type<select id="t_type" ${dis}><option value="flat" ${tr.tariff_type === 'flat' ? 'selected' : ''}>Flat</option><option value="time_of_use" ${tr.tariff_type === 'time_of_use' ? 'selected' : ''}>Time of use</option></select></label>${field('t_rate', 'Base rate per kWh', tr.rate_per_kwh, 'number', 'step=0.001 min=0')}${field('t_from', 'Peak window from', (tr.tou[0] || {}).from || '08:00', 'time')}${field('t_to', 'to', (tr.tou[0] || {}).to || '20:00', 'time')}${field('t_prate', 'Peak rate per kWh', (tr.tou[0] || {}).rate, 'number', 'step=0.001 min=0')}</div><p style="color:var(--muted)">Leave the rate empty if the tariff is unknown: cost is then shown as unavailable, never estimated.</p><button class="btn" id="s_tariff" ${dis}>Save tariff</button></div>
      <div class="panel"><h3>Thresholds & freshness</h3><div class="formgrid">${field('h_co2w', 'CO₂ warning (ppm)', th.co2.warn)}${field('h_co2c', 'CO₂ critical (ppm)', th.co2.crit)}${field('h_tw', 'Temperature warning (°C)', th.temperature.warn)}${field('h_tc', 'Temperature critical (°C)', th.temperature.crit)}${field('h_pw', 'PM2.5 warning (µg/m³)', th.pm25.warn)}${field('h_pc', 'PM2.5 critical (µg/m³)', th.pm25.crit)}${field('h_hw', 'Humidity warning (%)', th.humidity.warn)}${field('h_hc', 'Humidity critical (%)', th.humidity.crit)}${field('h_fresh', 'Freshness interval (s)', c.freshness_seconds)}${field('h_tz', 'Timezone', c.timezone, 'text')}${field('h_hm', 'Heatmap medium ≥ %', c.heatmap.medium)}${field('h_hh', 'Heatmap high ≥ %', c.heatmap.high)}<label class="f">Data-source labels<select id="h_lbl" ${dis}><option value="true" ${c.show_source_labels ? 'selected' : ''}>Show</option><option value="false" ${c.show_source_labels ? '' : 'selected'}>Hide</option></select></label></div><button class="btn" id="s_th" style="margin-top:10px" ${dis}>Save thresholds</button></div></div>
      <div class="panel" style="margin-top:10px"><h3>Data generator &amp; alarm tests</h3>${m.simulator.allowed ? `<div class="formgrid"><label class="f">Running<select id="sim_on" ${dis}><option value="true" ${m.simulator.running ? 'selected' : ''}>Enabled</option><option value="false" ${m.simulator.running ? '' : 'selected'}>Paused</option></select></label>${field('sim_int', 'Interval (s)', m.simulator.interval, 'number', 'min=1 max=3600')}</div><div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap"><button class="btn" id="s_sim" ${dis}>Save</button><button class="btn sec" data-sc="safety_test" ${dis}>Run fire-alarm test (3 min)</button><button class="btn sec" data-sc="leak_test" ${dis}>Run water-leak test (3 min)</button><button class="btn sec" data-sc="none" ${dis}>Stop test</button></div>` : '<p style="color:var(--muted)">Production mode: no demo data is generated. Enable with PREVIEW_MODE or SIMULATION_ENABLED in the deployment configuration.</p>'}</div>
      <div class="grid two" style="margin-top:10px"><div class="panel"><h3>Room capacities</h3><div class="scroll"><table class="t"><thead><tr><th>Room</th><th>Capacity</th><th></th></tr></thead><tbody>${m.rooms.filter(r => r.room_number).map(r => `<tr><td>${esc(r.room_number)} ${esc(r.label)}</td><td><input type="number" min="1" style="width:80px" value="${r.capacity ?? ''}" data-cap="${r.room_id}" ${dis}> ${r.capacity_source === 'demo' && S.labels ? badge('simulated', 'demo') : ''}</td><td><button class="btn sec sm" data-savecap="${r.room_id}" ${dis}>Save</button></td></tr>`).join('') || '<tr><td colspan=3>No mapped rooms</td></tr>'}</tbody></table></div></div>
      <div class="panel"><h3>Campus building markers</h3><div class="scroll"><table class="t"><thead><tr><th>Building</th><th>Code</th><th>x (0–1)</th><th>y (0–1)</th><th>Confirmed</th><th></th></tr></thead><tbody>${campusData ? [...campusData.buildings, ...campusData.unplaced].map(b => `<tr data-bm="${b.building_id}"><td>${esc(b.name)}</td><td><input type="text" size=3 value="${esc(b.code || '')}" class="bc" ${dis}></td><td><input type="number" step=0.01 min=0 max=1 style="width:70px" class="bx" value="${b.x ?? ''}" ${dis}></td><td><input type="number" step=0.01 min=0 max=1 style="width:70px" class="by" value="${b.y ?? ''}" ${dis}></td><td><input type="checkbox" class="bk" ${b.confirmed ? 'checked' : ''} ${dis}></td><td><button class="btn sec sm bs" ${dis}>Save</button></td></tr>`).join('') : ''}</tbody></table></div><p style="color:var(--muted);font-size:11px">Letters come from the supplied campus drawing; the drawing does not say which real building each letter is. Confirm a marker once you have verified it.</p></div></div>`;
      const nv = id => { const v = $('#' + id).value; return v === '' ? null : +v; };
      const save = async (body, ok) => { try { await send('PUT', '/api/dashboards/settings', body); msg(ok || 'Saved', 'info'); S.meta = await api('/api/dashboards/meta'); S.tz = S.meta.settings.timezone; SBChart.setTz(S.tz); S.labels = S.meta.settings.show_source_labels !== false; } catch (e) { msg(e.message); } };
      if (!ADMIN) return;
      $('#s_tariff').onclick = () => { const r = nv('t_rate'), p = nv('t_prate'); save({ tariff: { currency: $('#t_cur').value, tariff_type: $('#t_type').value, rate_per_kwh: r, tou: $('#t_type').value === 'time_of_use' && p != null ? [{ from: $('#t_from').value, to: $('#t_to').value, rate: p }] : [] } }, 'Tariff saved'); };
      $('#s_th').onclick = () => save({ thresholds: { ...th, co2: { warn: nv('h_co2w'), crit: nv('h_co2c') }, temperature: { ...th.temperature, warn: nv('h_tw'), crit: nv('h_tc') }, pm25: { warn: nv('h_pw'), crit: nv('h_pc') }, humidity: { ...th.humidity, warn: nv('h_hw'), crit: nv('h_hc') } }, freshness_seconds: nv('h_fresh'), timezone: $('#h_tz').value, heatmap: { medium: nv('h_hm'), high: nv('h_hh') }, show_source_labels: $('#h_lbl').value === 'true' }, 'Settings saved');
      const ss = $('#s_sim'); if (ss) ss.onclick = () => save({ sim_enabled: $('#sim_on').value === 'true', sim_interval: nv('sim_int') }, 'Simulator settings saved');
      document.querySelectorAll('[data-sc]').forEach(b => b.onclick = async () => { try { await send('POST', '/api/dashboards/simulator/scenario', { name: b.dataset.sc, minutes: 3 }); msg(b.dataset.sc === 'none' ? 'Test stopped' : 'Test started – alarms appear within a few seconds and clear automatically after 3 minutes', 'info'); } catch (e) { msg(e.message); } });
      document.querySelectorAll('[data-savecap]').forEach(b => b.onclick = async () => { const id = b.dataset.savecap, v = $(`[data-cap="${id}"]`).value; try { await send('PUT', `/api/dashboards/rooms/${id}/capacity`, { capacity: v === '' ? null : +v }); msg('Capacity saved', 'info'); } catch (e) { msg(e.message); } });
      document.querySelectorAll('.bs').forEach(b => b.onclick = async () => { const r = b.closest('tr'); try { await send('PUT', `/api/dashboards/buildings/${r.dataset.bm}/marker`, { code: r.querySelector('.bc').value, x: +r.querySelector('.bx').value, y: +r.querySelector('.by').value, confirmed: r.querySelector('.bk').checked }); msg('Marker saved', 'info'); } catch (e) { msg(e.message); } });
    },
    update() { }
  };

  /* ---------------- boot ---------------- */
  (async function boot() {
    try { S.meta = await api('/api/dashboards/meta'); } catch (e) { app.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; return; }
    S.tz = S.meta.settings.timezone; SBChart.setTz(S.tz); S.labels = S.meta.settings.show_source_labels !== false;
    PAGES[PAGE].init(); renderFilters(); syncNav();
    if (!PAGES[PAGE].live) {
      const st = $('#status'); st.className = 'pill s-ok'; st.lastChild.textContent = !S.labels ? 'Online' : S.meta.mode === 'production' ? 'Production' : 'Demo mode: ' + S.meta.mode; $('#srcpill').textContent = S.labels ? 'Source: ' + (SRC[S.meta.source] || S.meta.source) : ''; $('#srcpill').style.display = S.labels ? '' : 'none';
      await PAGES[PAGE].setup(); return;
    }
    Poll.run();
  })();
})();
