/* CargoMind mission control. Vanilla JS, no build step.
 * Talks to the FastAPI backend in cargomind/web/server.py:
 *   chat over SSE (POST /api/chat), live self-healing journal (EventSource),
 *   10-second polling of KPIs / shipments / fleet, feedback + lessons loop.
 */
(() => {
  'use strict';

  // ----------------------------------------------------------------- utils
  const $ = (sel, root = document) => root.querySelector(sel);
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const store = {
    get(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const rootCss = getComputedStyle(document.documentElement);
  const cssVar = (name, fallback) => rootCss.getPropertyValue(name).trim() || fallback;

  const STATUS = {
    exception: { label: 'Exception', rank: 0 },
    delayed: { label: 'Delayed', rank: 1 },
    out_for_delivery: { label: 'Out for delivery', rank: 2 },
    in_transit: { label: 'In transit', rank: 3 },
    picked_up: { label: 'Picked up', rank: 4 },
    created: { label: 'Created', rank: 5 },
    delivered: { label: 'Delivered', rank: 6 },
  };
  const STATUS_COLOR = Object.fromEntries(Object.keys(STATUS).map((s) => [s, cssVar(`--st-${s}`, '#8b9bb0')]));
  const MOVING = new Set(['picked_up', 'in_transit', 'out_for_delivery', 'delayed', 'exception']);
  const FLOWING = new Set(['picked_up', 'in_transit', 'out_for_delivery']);
  const PRIO_RANK = { critical: 0, express: 1, standard: 2 };
  const statusLabel = (s) => (STATUS[s] ? STATUS[s].label : String(s || 'unknown').replace(/_/g, ' '));

  const KIND_ORDER = ['fault', 'retry', 'fallback', 'breaker', 'arg_repair', 'reflect', 'recovered'];
  const KIND_LABEL = { fault: 'fault', retry: 'retry', fallback: 'fallback', breaker: 'breaker', arg_repair: 'arg repair', reflect: 'reflect', recovered: 'recovered' };

  const GLYPH = {
    cyclone: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 8.5c-1.6-3.6-6.2-5.6-10.2-4.1C6.6 6 4.7 10.4 6.4 13.9c1.6 3.2 5.9 4.2 8.6 2 2.3-1.9 2-5.6-.6-6.7-2.2-.9-4.7.6-4.4 2.9M3 15.5c1.6 3.6 6.2 5.6 10.2 4.1" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
    flood: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 15c2 0 2-1.6 4-1.6S8 15 10 15s2-1.6 4-1.6 2 1.6 4 1.6 2-1.6 4-1.6M2 20c2 0 2-1.6 4-1.6S8 20 10 20s2-1.6 4-1.6 2 1.6 4 1.6 2-1.6 4-1.6M12 3v7m-3-3 3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    closure: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9h18v5H3zM6 14v6M18 14v6M6 9V5M18 9V5M8 9l3 5M13 9l3 5" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    strike: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/><path d="M7.5 12h9" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
  };
  const SCENARIOS = [
    { event: 'Cyclone', city: 'Chennai', glyph: 'cyclone', coord: [13.0827, 80.2707], radiusKm: 180 },
    { event: 'Flood', city: 'Mumbai', glyph: 'flood', coord: [19.076, 72.8777], radiusKm: 90 },
    { event: 'Highway closure', city: 'Pune', glyph: 'closure', coord: [18.5204, 73.8567], radiusKm: 60 },
    { event: 'Transport strike', city: 'Kolkata', glyph: 'strike', coord: [22.5726, 88.3639], radiusKm: 110 },
  ];
  const autopilotPrompt = (sc) => `${sc.event} alert in ${sc.city}, severity high — run disruption autopilot`;

  // prompts that produce customer-facing text get the selected notice language appended
  const COMMS_RE = /\b(notice|notif\w*|message|e-?mail|sms|whatsapp|draft\w*|apolog\w*|letter|inform\w* (the )?customer|customer update)\b/i;
  const HAS_LANG_RE = /\b(in|into)\s+(hindi|tamil|english|marathi|bengali|telugu|kannada)\b/i;

  const QUICK = [
    'Where is SHP-1003?',
    "Plan today's dispatch from Mumbai",
    'Any weather risk on Mumbai → Pune?',
    'Which SKUs need reordering?',
    'Quote carriers Delhi → Bengaluru 800kg express',
    'Draft a delay notice for SHP-1007',
    'Morning ops briefing',
  ];

  const ICON = {
    up: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10v12M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 14V2M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    check: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M20 6 9 17l-5-5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    x: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    camera: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" style="vertical-align:-2px;margin-right:4px"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="12" cy="13" r="3" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
  };

  function toDate(v) {
    if (v === null || v === undefined || v === '') return null;
    if (v instanceof Date) return isNaN(v) ? null : v;
    if (typeof v === 'number') return new Date(v < 1e12 ? v * 1000 : v);
    const s = String(v).trim();
    if (/^\d+(\.\d+)?$/.test(s)) return toDate(Number(s));
    const d = new Date(/[T ]\d/.test(s) ? s.replace(' ', 'T') : s);
    return isNaN(d) ? null : d;
  }
  const fmtClock = (d) => (d ? d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '');
  const fmtHM = (d) => (d ? d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false }) : '');
  const fmtEta = (d) => (d ? d.toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) : '—');

  const pretty = (s) => String(s || '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim().replace(/\b\w/g, (c) => c.toUpperCase());
  const isRootAuthor = (a) => !a || /^(cargomind([_ ]?(ops|concierge))?|concierge|root(_agent)?|ops|coordinator|model|system|runtime)$/i.test(String(a));
  const agentLabel = (a) => (isRootAuthor(a) ? 'CargoMind' : pretty(String(a).replace(/_agent$/i, '')));

  function compact(v, max = 280) {
    let s;
    if (typeof v === 'string') s = v;
    else { try { s = JSON.stringify(v); } catch { s = String(v); } }
    if (s === undefined) s = String(v);
    return s.length > max ? `${s.slice(0, max - 1)}…` : s;
  }
  function prettyJson(v) {
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v, null, 2); } catch { return String(v); }
  }

  function countBy(list, fn) {
    const out = {};
    for (const x of list) { const k = fn(x); if (k !== undefined && k !== null) out[k] = (out[k] || 0) + 1; }
    return out;
  }

  async function api(path, opts = {}) {
    const init = { ...opts, headers: { Accept: 'application/json', ...(opts.headers || {}) } };
    if (opts.json !== undefined) {
      init.body = JSON.stringify(opts.json);
      init.headers['Content-Type'] = 'application/json';
      init.method = init.method || 'POST';
      delete init.json;
    }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      const msg = (data && (data.error || data.detail)) || `HTTP ${res.status}`;
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function toast(msg, kind = 'info', ms = 4200) {
    const box = $('#toasts');
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = msg;
    box.append(el);
    while (box.children.length > 4) box.firstElementChild.remove();
    setTimeout(() => { el.classList.add('is-out'); setTimeout(() => el.remove(), 320); }, ms);
  }

  // ----------------------------------------------------------------- state
  const state = {
    session: '',
    cities: {}, citiesLower: {},
    shipments: [], warehouses: [], vehicles: [], lowStock: [], kpis: null,
    shipSig: '', laneSig: '', tableSig: '',
    prevKpi: {},
    filters: { status: '', city: '', q: '' },
    selected: null, hot: new Set(),
    streaming: false, abort: null, attachment: null,
    heal: { counts: Object.fromEntries(KIND_ORDER.map((k) => [k, 0])), seen: new Set(), es: null, wasDown: false },
    lessons: [],
    health: null,
    lang: '',
    models: { counts: {}, replies: 0, recent: [] },
    alerts: { seen: new Set(), loaded: false },
    incident: null,
    map: null, layers: null, lanes: new Map(), mapFitted: false, citiesDrawn: false,
  };

  // ================================================================= HEADER
  function tickClock() {
    const now = new Date();
    $('#clock').textContent = now.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false });
    $('#clock-date').textContent = now.toLocaleDateString('en-GB', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' });
  }

  async function loadHealth() {
    try {
      const h = await api('/api/health');
      state.health = h;
      renderModes(h);
      if (typeof h.chaos_rate === 'number') setChaosUI(Math.round(h.chaos_rate * 100), true);
      else await loadChaos();
    } catch (e) {
      $('#modes').innerHTML = '<span class="mode-pill is-off" role="listitem"><i class="dot"></i><span class="mode-text"><b>Backend unreachable</b><small>retrying…</small></span></span>';
    }
  }

  function renderModes(h) {
    const m = h.mode || {};
    const models = m.models || {};
    const c = h.components || {};
    const anyModel = !!(m.gemini || m.claude || m.openai);
    const pills = [
      { label: 'Agents', sub: c.runtime ? 'ADK multi-agent' : 'runtime not loaded', state: c.runtime ? 'live' : 'off', tip: 'Google ADK agent team (router + specialists)' },
      { label: 'Gemini Flash', sub: models.fast, state: m.gemini ? 'live' : 'off', tip: 'Fast conversational and routing model' },
      { label: 'Gemini Pro', sub: models.pro, state: m.gemini ? 'live' : 'off', tip: 'Deep reasoning for dispatch planning' },
      { label: 'Vision', sub: models.vision, state: m.gemini ? 'live' : 'off', tip: 'Multimodal proof-of-delivery inspection' },
      { label: 'Claude', sub: String(models.claude || '').replace(/^anthropic\//, ''), state: m.claude ? 'live' : 'off', tip: 'Second-vendor fallback and independent plan critic' },
    ];
    if (m.openai) pills.push({ label: 'OpenAI', sub: 'cross-vendor fallback', state: 'live', tip: 'Additional fallback vendor' });
    pills.push(
      { label: 'Maps', sub: m.google_maps ? 'Google Routes' : 'OSRM · estimate', state: m.google_maps ? 'live' : 'off', tip: 'Routing and geocoding provider chain' },
      { label: 'MCP', sub: 'logistics tools', state: c.mcp_server ? 'live' : 'off', tip: 'Model Context Protocol server exposing the logistics DB' },
      { label: 'Fallback', sub: anyModel ? 'rule-engine armed' : 'serving all traffic', state: 'armed', tip: 'Offline rule engine: always-on last link in every model chain' },
    );
    $('#modes').innerHTML = pills.map((p) => {
      const word = p.state === 'live' ? 'live' : p.state === 'armed' ? 'armed' : 'offline';
      return `<span class="mode-pill is-${p.state}" role="listitem" title="${esc(`${p.label}: ${word}. ${p.tip}`)}" aria-label="${esc(`${p.label} ${word}`)}">` +
        `<i class="dot"></i><span class="mode-text"><b>${esc(p.label)}</b><small>${esc(p.sub || word)}</small></span></span>`;
    }).join('');
  }

  // ---------------------------------------------------------- chaos monkey
  const chaosRange = () => $('#chaos-range');
  function setChaosUI(pct, fromServer = false) {
    const input = chaosRange();
    const v = Math.max(0, Math.min(60, pct));
    if (fromServer && document.activeElement === input) return;
    input.value = String(v);
    input.style.setProperty('--pct', `${(v / 60) * 100}%`);
    $('#chaos-value').textContent = `${v}%`;
    $('#chaos').classList.toggle('is-armed', v > 0);
    $('#topbar').classList.toggle('is-chaos', v > 0);
    input.setAttribute('aria-valuetext', v > 0 ? `${v} percent of tool calls fail` : 'off');
  }
  async function loadChaos() {
    try {
      const r = await api('/api/chaos');
      setChaosUI(Math.round((r.rate || 0) * 100), true);
      $('#chaos').classList.remove('is-disabled');
      chaosRange().disabled = false;
    } catch {
      $('#chaos').classList.add('is-disabled');
      chaosRange().disabled = true;
      $('#chaos').title = 'Self-healing module not loaded yet';
    }
  }
  let chaosTimer = null;
  function onChaosInput() {
    const v = Number(chaosRange().value);
    setChaosUI(v);
    clearTimeout(chaosTimer);
    chaosTimer = setTimeout(() => pushChaos(v), 250);
  }
  async function pushChaos(v) {
    try {
      const r = await api('/api/chaos', { json: { rate: v / 100 } });
      const pct = Math.round((r.rate || 0) * 100);
      if (pct > 0) {
        toast(`Chaos Monkey armed: about ${pct}% of tool calls will now fail. Watch the agents heal.`, 'warn');
        setDrawer(true);
      } else {
        toast('Chaos Monkey disarmed. Tools are back to normal.', 'ok');
      }
    } catch (e) {
      toast(`Could not set chaos rate: ${e.message}`, 'err');
    }
  }

  // ================================================================= KPIs
  function flatten(obj, prefix = '', out = {}) {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return out;
    for (const [k, v] of Object.entries(obj)) {
      const key = prefix ? `${prefix}.${k}` : k;
      if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, key, out);
      else {
        out[key] = v;
        if (!(k in out)) out[k] = v;
      }
    }
    return out;
  }
  function pick(flat, keys) {
    for (const k of keys) {
      const v = flat[k];
      if (v !== undefined && v !== null && v !== '') return Array.isArray(v) ? v.length : v;
    }
    return undefined;
  }
  function num(v) {
    if (typeof v === 'number' && isFinite(v)) return v;
    if (typeof v === 'string') { const n = parseFloat(v.replace(/[%,]/g, '')); return isNaN(n) ? undefined : n; }
    return undefined;
  }
  function pct(v) {
    const n = num(v);
    if (n === undefined) return undefined;
    const explicit = typeof v === 'string' && v.includes('%');
    return !explicit && n >= 0 && n <= 1 ? n * 100 : n;
  }

  const KPI_TILES = [
    { id: 'ontime', label: 'On-time' },
    { id: 'transit', label: 'In transit' },
    { id: 'delayed', label: 'Delayed' },
    { id: 'exceptions', label: 'Exceptions' },
    { id: 'lowstock', label: 'Low stock' },
    { id: 'fleet', label: 'Fleet util.' },
  ];

  function buildKpiTiles() {
    $('#kpi-grid').innerHTML = KPI_TILES.map((t) => `
      <button type="button" class="kpi" data-kpi="${t.id}">
        <span class="kpi-label">${esc(t.label)}</span>
        <span class="kpi-value"><span class="v">—</span><small class="u"></small><span class="kpi-delta"></span></span>
        <span class="kpi-bar" hidden><i></i></span>
        <span class="kpi-foot">&nbsp;</span>
      </button>`).join('');
  }

  function computeKpis() {
    const flat = flatten(state.kpis || {});
    const ships = state.shipments;
    const total = ships.length;
    const by = countBy(ships, (s) => s.status);
    const active = total - (by.delivered || 0);
    const vehicles = state.vehicles;
    const vtotal = vehicles.length;
    const vbusy = vehicles.filter((v) => !/idle|available|free|parked|maintenance|off|inactive/i.test(String(v.status || ''))).length;

    let onTime = pct(pick(flat, ['on_time_pct', 'on_time_percent', 'on_time_rate', 'on_time_percentage', 'otd_pct', 'on_time_delivery_pct', 'on_time_delivery_rate', 'otif_pct', 'on_time']));
    if (onTime === undefined && total) onTime = (100 * (total - (by.delayed || 0) - (by.exception || 0))) / total;
    const inTransit = num(pick(flat, ['in_transit', 'in_transit_count', 'shipments_in_transit'])) ?? (by.in_transit || 0);
    const delayed = num(pick(flat, ['delayed', 'delayed_count', 'shipments_delayed', 'delayed_shipments'])) ?? (by.delayed || 0);
    const exceptions = num(pick(flat, ['exceptions', 'exception', 'exception_count', 'open_exceptions', 'shipments_exception'])) ?? (by.exception || 0);
    const lowStock = num(pick(flat, ['low_stock', 'low_stock_skus', 'low_stock_count', 'skus_low_stock', 'below_reorder', 'below_reorder_point', 'reorder_needed'])) ?? state.lowStock.length;
    let fleet = pct(pick(flat, ['fleet_utilisation', 'fleet_utilization', 'fleet_utilisation_pct', 'fleet_utilization_pct', 'utilisation_pct', 'utilization_pct', 'vehicle_utilisation', 'vehicle_utilization']));
    if (fleet === undefined && vtotal) fleet = (100 * vbusy) / vtotal;
    const netTotal = num(pick(flat, ['total_shipments', 'shipments_total', 'shipment_count', 'total'])) ?? total;

    return {
      ontime: { v: onTime, unit: '%', bar: onTime, tone: onTime === undefined ? 'info' : onTime >= 90 ? 'ok' : onTime >= 75 ? 'warn' : 'bad', foot: `${active} active of ${netTotal}`, tip: 'Share of shipments on track to meet their promise. Click to clear filters.' },
      transit: { v: inTransit, tone: 'info', foot: `${by.out_for_delivery || 0} out for delivery`, tip: 'Shipments moving now. Click to filter the table and map.' },
      delayed: { v: delayed, tone: delayed > 0 ? 'bad' : 'ok', foot: netTotal ? `${Math.round((100 * (delayed || 0)) / netTotal)}% of network` : '', tip: 'Delayed shipments. Click to filter.' },
      exceptions: { v: exceptions, tone: exceptions > 0 ? 'exc' : 'ok', foot: exceptions > 0 ? 'need attention' : 'all clear', tip: 'Shipments in exception. Click to filter.' },
      lowstock: { v: lowStock, tone: lowStock > 0 ? 'warn' : 'ok', foot: lowStock > 0 ? 'below reorder pt' : 'stock healthy', tip: 'SKU lines below their reorder point. Click to ask the agent.' },
      fleet: { v: fleet, unit: '%', bar: fleet, tone: fleet === undefined ? 'info' : fleet >= 90 ? 'warn' : 'info', foot: vtotal ? `${vbusy}/${vtotal} on duty` : 'fleet pending', tip: 'Vehicles on duty. Click to toggle the fleet layer on the map.' },
    };
  }

  function renderKpis() {
    const k = computeKpis();
    for (const t of KPI_TILES) {
      const tile = $(`.kpi[data-kpi="${t.id}"]`);
      if (!tile) continue;
      const d = k[t.id];
      const has = d.v !== undefined && d.v !== null && !isNaN(d.v);
      const shown = has ? (d.unit === '%' ? Math.round(d.v * 10) / 10 : Math.round(d.v)) : null;
      const text = shown === null ? '—' : d.unit === '%' ? (shown >= 99.95 ? '100' : shown.toFixed(shown % 1 ? 1 : 0)) : String(shown);
      tile.className = `kpi tone-${d.tone}`;
      $('.v', tile).textContent = text;
      $('.u', tile).textContent = has && d.unit ? d.unit : '';
      $('.kpi-foot', tile).textContent = d.foot || ' ';
      const bar = $('.kpi-bar', tile);
      bar.hidden = d.bar === undefined;
      if (d.bar !== undefined) $('i', bar).style.width = `${Math.max(0, Math.min(100, d.bar))}%`;
      tile.setAttribute('aria-label', `${t.label}: ${text}${has && d.unit ? d.unit : ''}. ${d.foot || ''}`);
      tile.title = d.tip || '';
      const prev = state.prevKpi[t.id];
      const delta = $('.kpi-delta', tile);
      if (prev !== undefined && shown !== null && prev !== shown) {
        const diff = Math.round((shown - prev) * 10) / 10;
        delta.textContent = `${diff > 0 ? '+' : '−'}${Math.abs(diff)}`;
        delta.className = `kpi-delta ${diff > 0 ? 'up' : 'down'}`;
        tile.classList.remove('flash');
        void tile.offsetWidth; // restart animation
        tile.classList.add('flash');
        clearTimeout(tile._deltaTimer);
        tile._deltaTimer = setTimeout(() => { delta.textContent = ''; }, 8000);
      }
      if (shown !== null) state.prevKpi[t.id] = shown;
    }
  }

  function onKpiClick(id) {
    switch (id) {
      case 'transit': setStatusFilter('in_transit'); break;
      case 'delayed': setStatusFilter('delayed'); break;
      case 'exceptions': setStatusFilter('exception'); break;
      case 'ontime': setStatusFilter(''); break;
      case 'lowstock': sendMessage('Which SKUs need reordering?'); break;
      case 'fleet': toggleFleetLayer(); break;
      default: break;
    }
  }

  // ================================================================= SHIPMENTS
  function setStatusFilter(v) {
    state.filters.status = v;
    $('#f-status').value = v;
    applyFilters();
  }

  function filteredShipments() {
    const { status, city, q } = state.filters;
    const qq = q.trim().toLowerCase();
    const cityL = city.toLowerCase();
    return state.shipments.filter((s) =>
      (!status || s.status === status) &&
      (!cityL || [s.origin_city, s.destination_city, s.last_city].some((c) => String(c || '').toLowerCase() === cityL)) &&
      (!qq || [s.id, s.customer, s.origin_city, s.destination_city, s.order_id, s.carrier_id, s.vehicle_id, s.status]
        .some((v) => String(v || '').toLowerCase().includes(qq))));
  }

  const rankOf = (s) => (STATUS[s.status] ? STATUS[s.status].rank : 9);
  function sortShipments(list) {
    return [...list].sort((a, b) => rankOf(a) - rankOf(b)
      || (PRIO_RANK[a.priority] ?? 3) - (PRIO_RANK[b.priority] ?? 3)
      || String(a.id).localeCompare(String(b.id), undefined, { numeric: true }));
  }

  function etaCell(s) {
    const eta = toDate(s.eta);
    const promised = toDate(s.promised_by);
    if (s.status === 'delivered') return `<span title="Delivered">${esc(fmtEta(toDate(s.last_update) || eta))}</span>`;
    let late = '';
    let hrs = num(s.delay_hours);
    if (hrs === undefined && eta && promised) hrs = (eta - promised) / 3600000;
    if (hrs !== undefined && hrs > 0.25 && s.is_late !== false) {
      late = `<span class="late">+${hrs >= 10 ? Math.round(hrs) : hrs.toFixed(1)}h vs promise</span>`;
    }
    return `${esc(fmtEta(eta))}${late}`;
  }

  function renderTable(force = false) {
    const rows = sortShipments(filteredShipments());
    const sig = JSON.stringify([state.filters, state.selected, rows.map((s) => [s.id, s.status, s.eta, s.priority, s.last_city])]);
    if (!force && sig === state.tableSig) return;
    state.tableSig = sig;
    const body = $('#ship-body');
    const total = state.shipments.length;
    $('#ship-count').textContent = rows.length === total ? `${total} tracked` : `${rows.length} of ${total}`;
    if (!rows.length) {
      body.innerHTML = `<tr class="empty-row"><td colspan="4">${total ? 'No shipments match these filters.' : 'No shipments loaded yet.'}</td></tr>`;
      return;
    }
    body.innerHTML = rows.map((s) => {
      const prio = s.priority && s.priority !== 'standard' ? s.priority : 'standard';
      return `<tr data-id="${esc(s.id)}" tabindex="0" class="${s.id === state.selected ? 'is-selected' : ''}" aria-label="${esc(`${s.id}, ${s.origin_city} to ${s.destination_city}, ${statusLabel(s.status)}`)}">
        <td><span class="ship-id"><i class="prio p-${esc(prio)}" title="${esc(prio)} priority"></i>${esc(s.id)}</span><span class="ship-sub" title="${esc(s.customer || '')}">${esc(s.customer || s.order_id || '')}</span></td>
        <td class="lane-cell">${esc(s.origin_city || '?')}<span class="arrow" aria-hidden="true">→</span>${esc(s.destination_city || '?')}${s.last_city && MOVING.has(s.status) ? `<span class="ship-sub">at ${esc(s.last_city)}</span>` : ''}</td>
        <td><span class="chip st-${esc(s.status)}">${esc(statusLabel(s.status))}</span></td>
        <td class="eta-cell">${etaCell(s)}</td>
      </tr>`;
    }).join('');
  }

  function populateCityFilter() {
    const sel = $('#f-city');
    const set = new Set();
    for (const s of state.shipments) [s.origin_city, s.destination_city].forEach((c) => c && set.add(c));
    for (const w of state.warehouses) if (w.city) set.add(w.city);
    const list = [...set].sort();
    const sig = list.join('|');
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    const cur = sel.value;
    sel.innerHTML = '<option value="">All cities</option>' + list.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    sel.value = list.includes(cur) ? cur : '';
  }

  function applyFilters() {
    renderTable(true);
    applyLaneFilter();
  }

  function selectShipment(id, { fly = true, fromMap = false } = {}) {
    state.selected = id;
    renderTable(true);
    const row = $(`#ship-body tr[data-id="${CSS.escape(id)}"]`);
    if (row && !fromMap) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    else if (row) row.scrollIntoView({ block: 'nearest' });
    setHot([id], 0);
    if (fly && !fromMap) focusShipment(id);
  }

  // ================================================================= MAP
  function coord(city) {
    if (!city) return null;
    const c = state.cities[city] || state.citiesLower[String(city).toLowerCase()];
    return c ? [Number(c[0]), Number(c[1])] : null;
  }
  function placeCoord(o) {
    const lat = o.lat ?? o.latitude;
    const lon = o.lon ?? o.lng ?? o.longitude;
    if (lat !== undefined && lon !== undefined && !isNaN(lat) && !isNaN(lon)) return [Number(lat), Number(lon)];
    return coord(o.city || o.current_city || o.location || o.base_city || o.home_city);
  }

  function initMap() {
    if (!window.L) {
      const off = $('#map-offline');
      off.hidden = false;
      off.textContent = 'Map library could not load (offline?). Everything else still works.';
      return;
    }
    const map = L.map('map', { zoomControl: false, minZoom: 3, maxZoom: 13, zoomSnap: 0.25, worldCopyJump: true })
      .setView([21.5, 79.5], 5);
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    const tiles = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      subdomains: 'abcd',
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    }).addTo(map);
    let tileErrors = 0;
    tiles.on('tileerror', () => { if (++tileErrors >= 6) $('#map-offline').hidden = false; });
    tiles.on('tileload', () => { tileErrors = 0; $('#map-offline').hidden = true; });

    const layers = {
      glow: L.layerGroup().addTo(map),
      lanes: L.layerGroup().addTo(map),
      movers: L.layerGroup().addTo(map),
      cities: L.layerGroup().addTo(map),
      hubs: L.layerGroup().addTo(map),
      fleet: L.layerGroup(),
    };
    state.layers = layers;
    state.map = map;
    L.control.layers(null, {
      'Shipment lanes': layers.lanes,
      'Lane glow': layers.glow,
      'Shipments in motion': layers.movers,
      'Warehouses': layers.hubs,
      'Cities': layers.cities,
      'Fleet': layers.fleet,
    }, { position: 'topright', collapsed: true }).addTo(map);

    map.on('popupopen', (e) => {
      const el = e.popup.getElement();
      if (!el) return;
      el.querySelectorAll('[data-ask]').forEach((b) => b.addEventListener('click', () => {
        map.closePopup();
        sendMessage(b.dataset.ask);
      }));
    });
    let t = null;
    window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => map.invalidateSize(), 150); });
  }

  function bezier(a, b, bend, n = 36) {
    const [y1, x1] = a; const [y2, x2] = b;
    const mx = (x1 + x2) / 2; const my = (y1 + y2) / 2;
    const dx = x2 - x1; const dy = y2 - y1;
    const cx = mx - dy * bend; const cy = my + dx * bend;
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n; const u = 1 - t;
      pts.push([u * u * y1 + 2 * u * t * cy + t * t * y2, u * u * x1 + 2 * u * t * cx + t * t * x2]);
    }
    return pts;
  }

  function moverPoint(s, pts) {
    const at = coord(s.last_city);
    if (at) {
      let best = 0; let bestD = Infinity;
      pts.forEach((p, i) => { const d = (p[0] - at[0]) ** 2 + (p[1] - at[1]) ** 2; if (d < bestD) { bestD = d; best = i; } });
      const idx = Math.min(Math.max(best, 3), pts.length - 4);
      return pts[idx];
    }
    const frac = { picked_up: 0.12, in_transit: 0.5, out_for_delivery: 0.86, delayed: 0.45, exception: 0.55 }[s.status] ?? 0.5;
    return pts[Math.round(frac * (pts.length - 1))];
  }

  function shipmentPopup(s) {
    const row = (k, v) => (v || v === 0 ? `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>` : '');
    const st = s.status;
    const asks = [`<button type="button" class="pop-btn" data-ask="${esc(`Where is ${s.id} and will it meet its promised time?`)}">Ask agent</button>`];
    if (st === 'delayed' || st === 'exception') asks.push(`<button type="button" class="pop-btn" data-ask="${esc(`Draft a delay notice for ${s.id}`)}">Draft notice</button>`);
    if (MOVING.has(st)) asks.push(`<button type="button" class="pop-btn" data-ask="${esc(`Any weather risk on ${s.origin_city} → ${s.destination_city}?`)}">Lane risk</button>`);
    return `<div class="pop">
      <div class="pop-title"><b>${esc(s.id)}</b><span class="chip st-${esc(st)}">${esc(statusLabel(st))}</span>${s.priority && s.priority !== 'standard' ? `<span class="flag flag-degraded">${esc(s.priority)}</span>` : ''}</div>
      <div class="pop-lane">${esc(s.origin_city)} → ${esc(s.destination_city)}</div>
      <dl class="pop-grid">
        ${row('Customer', s.customer)}${row('Contents', s.contents)}${row('Weight', s.weight_kg ? `${s.weight_kg} kg` : '')}
        ${row('Carrier', s.carrier_name || s.carrier_id)}${row('Vehicle', s.vehicle_id)}${row('Last seen', s.last_city)}
        ${row('ETA', s.eta ? fmtEta(toDate(s.eta)) : '')}${row('Promised', s.promised_by ? fmtEta(toDate(s.promised_by)) : '')}
      </dl>
      ${s.notes ? `<p class="pop-note">${esc(s.notes)}</p>` : ''}
      <div class="pop-actions">${asks.join('')}</div>
    </div>`;
  }

  function drawLanes() {
    if (!state.map) return;
    const sig = JSON.stringify(state.shipments.map((s) => [s.id, s.status, s.origin_city, s.destination_city, s.last_city, s.priority]));
    if (sig === state.laneSig && state.lanes.size) return;
    state.laneSig = sig;
    const { lanes, movers, glow } = state.layers;
    lanes.clearLayers(); movers.clearLayers(); glow.clearLayers();
    state.lanes.clear();

    const pairCount = {};
    const ordered = sortShipments(state.shipments).reverse(); // delivered first, critical on top
    const bounds = [];
    for (const s of ordered) {
      const a = coord(s.origin_city); const b = coord(s.destination_city);
      if (!a || !b) continue;
      const [ka, kb] = [s.origin_city, s.destination_city].sort();
      const key = `${ka}|${kb}`;
      const n = (pairCount[key] = (pairCount[key] || 0) + 1);
      const mag = 0.14 + Math.floor((n - 1) / 2) * 0.1;
      const sign = n % 2 ? 1 : -1;
      const flipped = s.origin_city !== ka;
      let pts = bezier(flipped ? b : a, flipped ? a : b, mag * sign);
      if (flipped) pts = pts.reverse();
      const st = s.status;
      const color = STATUS_COLOR[st] || '#8b9bb0';
      const delivered = st === 'delivered';

      const glowLine = L.polyline(pts, { color, weight: 6, opacity: delivered ? 0 : 0.1, interactive: false, className: `lane-glow lane-glow--${st}` }).addTo(glow);
      const base = L.polyline(pts, {
        color, weight: delivered ? 1.3 : st === 'created' ? 1.6 : 2.4, opacity: delivered ? 0.3 : 0.88,
        interactive: false, className: `lane lane--${st}`, lineCap: 'round',
      }).addTo(lanes);
      const flow = FLOWING.has(st)
        ? L.polyline(pts, { color: '#eaf4ff', weight: 2.2, opacity: 0.75, interactive: false, className: 'lane-flow' }).addTo(lanes)
        : null;
      const hit = L.polyline(pts, { color, weight: 16, opacity: 0, className: 'lane-hit' }).addTo(lanes);
      hit.bindTooltip(`<b>${esc(s.id)}</b> · ${esc(s.origin_city)} → ${esc(s.destination_city)} · ${esc(statusLabel(st))}`, { sticky: true, className: 'cm-tip', direction: 'top', offset: [0, -6] });
      hit.bindPopup(shipmentPopup(s), { maxWidth: 300, autoPanPadding: [40, 40] });
      hit.on('click', () => selectShipment(s.id, { fromMap: true }));
      hit.on('mouseover', () => lineClass({ base, glowLine }, 'is-hot', true));
      hit.on('mouseout', () => { if (!state.hot.has(s.id)) lineClass({ base, glowLine }, 'is-hot', false); });

      let mover = null;
      if (MOVING.has(st)) {
        const p = moverPoint(s, pts);
        mover = L.circleMarker(p, { radius: 4.5, color, weight: 3, opacity: 0.4, fillColor: color, fillOpacity: 1, className: `mover mover--${st}`, interactive: false }).addTo(movers);
      }
      state.lanes.set(s.id, { s, pts, base, flow, glowLine, hit, mover });
      bounds.push(a, b);
    }
    if (!state.mapFitted && bounds.length) {
      for (const w of state.warehouses) { const c = placeCoord(w); if (c) bounds.push(c); }
      state.map.fitBounds(L.latLngBounds(bounds), { padding: [36, 36], maxZoom: 6 });
      state.mapFitted = true;
    }
    applyLaneFilter();
    for (const id of state.hot) { const l = state.lanes.get(id); if (l) lineClass(l, 'is-hot', true); }
    renderLegend();
  }

  function lineClass(l, name, on) {
    for (const layer of [l.base, l.glowLine, l.flow, l.mover]) {
      const el = layer && layer.getElement && layer.getElement();
      if (el) el.classList.toggle(name, on);
    }
    if (name === 'is-hot' && on && l.base && l.base.bringToFront) { l.base.bringToFront(); if (l.flow) l.flow.bringToFront(); }
  }

  function applyLaneFilter() {
    if (!state.lanes.size) return;
    const f = state.filters;
    const active = f.status || f.city || f.q.trim();
    const keep = new Set(filteredShipments().map((s) => s.id));
    for (const [id, l] of state.lanes) lineClass(l, 'is-dim', !!active && !keep.has(id));
  }

  function setHot(ids, ttl = 7000) {
    for (const id of state.hot) { const l = state.lanes.get(id); if (l) lineClass(l, 'is-hot', false); }
    state.hot = new Set(ids);
    for (const id of ids) { const l = state.lanes.get(id); if (l) lineClass(l, 'is-hot', true); }
    clearTimeout(setHot._t);
    if (ttl) setHot._t = setTimeout(() => setHot(state.selected ? [state.selected] : [], 0), ttl);
  }

  function focusShipment(id, { popup = true } = {}) {
    const l = state.lanes.get(id);
    if (!l || !state.map) {
      const known = state.shipments.some((s) => s.id === id);
      toast(known ? `${id} has no mappable lane.` : `${id} is not in the current shipment list.`, 'warn');
      return;
    }
    state.map.flyToBounds(L.latLngBounds(l.pts), { padding: [70, 70], maxZoom: 7.5, duration: 0.9 });
    setHot([id], state.selected === id ? 0 : 7000);
    if (popup) setTimeout(() => l.hit.openPopup(l.pts[Math.floor(l.pts.length / 2)]), 950);
  }

  function focusMany(ids) {
    const lanes = ids.map((id) => state.lanes.get(id)).filter(Boolean);
    if (!lanes.length || !state.map) return;
    if (lanes.length === 1) { focusShipment(ids.find((id) => state.lanes.has(id)), { popup: false }); return; }
    const b = L.latLngBounds(lanes.flatMap((l) => [l.pts[0], l.pts[l.pts.length - 1]]));
    state.map.flyToBounds(b, { padding: [60, 60], maxZoom: 7, duration: 0.9 });
    setHot(ids, 8000);
  }

  function drawHubs() {
    if (!state.map) return;
    const { hubs } = state.layers;
    hubs.clearLayers();
    const lowBy = countBy(state.lowStock, (i) => i.warehouse_id || i.warehouse || i.wh_id);
    for (const w of state.warehouses) {
      const c = placeCoord(w);
      if (!c) continue;
      const id = w.id || w.warehouse_id || '';
      const code = String(id).replace(/^WH-/i, '') || String(w.city || '').slice(0, 3).toUpperCase();
      const low = lowBy[id] || num(w.low_stock_lines) || 0;
      const icon = L.divIcon({
        className: 'hub-icon',
        html: `<div class="hub">${esc(code)}${low ? `<i class="hub-alert">${low}</i>` : ''}</div>`,
        iconSize: [28, 28], iconAnchor: [14, 14], popupAnchor: [0, -14],
      });
      const rows = [
        ['City', w.city], ['Capacity', w.capacity ?? w.capacity_units ?? w.capacity_pallets],
        ['Utilisation', (() => { const u = w.utilisation ?? w.utilization ?? w.utilization_pct ?? w.utilisation_pct; return u === undefined || u === null ? undefined : `${u}%`; })()],
        ['Cold chain', w.cold_chain === undefined ? undefined : (w.cold_chain ? 'yes' : 'no')],
        ['Manager', w.manager], ['Low-stock SKUs', low],
      ].filter(([, v]) => v !== undefined && v !== null && v !== '');
      const pop = `<div class="pop">
        <div class="pop-title"><b>${esc(id)}</b></div>
        <div class="pop-lane">${esc(w.name || w.city || id)}</div>
        <dl class="pop-grid">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
        <div class="pop-actions">
          <button type="button" class="pop-btn" data-ask="${esc(`Which SKUs at ${id} are below their reorder point?`)}">Stock check</button>
          <button type="button" class="pop-btn" data-ask="${esc(`Plan today's dispatch from ${w.city || id}`)}">Plan dispatch</button>
        </div></div>`;
      L.marker(c, { icon, title: `${id} ${w.name || ''}`.trim(), zIndexOffset: 600, keyboard: true, riseOnHover: true })
        .bindPopup(pop, { maxWidth: 300 })
        .addTo(hubs);
    }
  }

  function drawCities() {
    if (!state.map || state.citiesDrawn || !Object.keys(state.cities).length) return;
    const { cities } = state.layers;
    const network = new Set();
    for (const s of state.shipments) { network.add(s.origin_city); network.add(s.destination_city); }
    for (const w of state.warehouses) network.add(w.city);
    for (const [name, c] of Object.entries(state.cities)) {
      const inNet = network.has(name);
      const m = L.circleMarker([c[0], c[1]], { radius: inNet ? 3.2 : 2.2, stroke: false, fillColor: inNet ? '#c9d4df' : '#5d6a7a', fillOpacity: 0.95, interactive: true });
      m.bindTooltip(name, inNet
        ? { permanent: true, direction: 'right', offset: [6, 0], className: 'city-label' }
        : { direction: 'top', className: 'cm-tip' });
      m.addTo(cities);
    }
    state.citiesDrawn = true;
  }

  function vehicleColor(status) {
    const s = String(status || '').toLowerCase();
    if (/maint|repair|down|breakdown/.test(s)) return STATUS_COLOR.delayed;
    if (/idle|available|free|parked/.test(s)) return '#8796a6';
    return STATUS_COLOR.in_transit;
  }
  function drawFleet() {
    if (!state.map) return;
    const { fleet } = state.layers;
    fleet.clearLayers();
    const perCity = {};
    for (const v of state.vehicles) {
      const c = placeCoord(v);
      if (!c) continue;
      const k = `${c[0]},${c[1]}`;
      const i = (perCity[k] = (perCity[k] || 0) + 1) - 1;
      const ang = i * 2.4; const r = 0.12 + 0.05 * i;
      const p = [c[0] + Math.sin(ang) * r, c[1] + Math.cos(ang) * r];
      const icon = L.divIcon({ className: 'truck-icon', html: `<div class="truck" style="--c:${vehicleColor(v.status)}"></div>`, iconSize: [12, 8] });
      const info = [v.id, v.type || v.vehicle_type, v.capacity_kg ? `${v.capacity_kg} kg` : '', v.status, v.driver || v.driver_name].filter(Boolean).join(' · ');
      L.marker(p, { icon, title: info, keyboard: false }).bindTooltip(esc(info), { className: 'cm-tip', direction: 'top' }).addTo(fleet);
    }
  }
  function toggleFleetLayer() {
    if (!state.map) return;
    const { fleet } = state.layers;
    if (state.map.hasLayer(fleet)) { state.map.removeLayer(fleet); toast('Fleet layer hidden.'); }
    else { state.map.addLayer(fleet); toast(`Fleet layer on: ${state.vehicles.length} vehicles plotted at their current city.`); }
  }

  function renderLegend() {
    const by = countBy(state.shipments, (s) => s.status);
    const items = Object.keys(STATUS).filter((s) => by[s]).sort((a, b) => STATUS[a].rank - STATUS[b].rank);
    const active = state.filters.status;
    $('#map-legend').innerHTML = items.map((s) =>
      `<button type="button" data-status="${s}" class="${active && active !== s ? 'is-off' : ''}" aria-pressed="${active === s}" title="Show only ${esc(statusLabel(s))}"><i style="--c:${STATUS_COLOR[s]}"></i>${esc(statusLabel(s))} <b>${by[s]}</b></button>`).join('') +
      `<span><i class="hub-key"></i>Warehouse</span>`;
    const moving = state.shipments.filter((s) => FLOWING.has(s.status)).length;
    $('#map-stats').textContent = `${state.lanes.size} lanes · ${state.warehouses.length} hubs · ${moving} moving`;
  }

  // ================================================================= DATA LOADING
  async function loadCities() {
    try {
      const c = await api('/api/cities');
      const map = c && c.cities && typeof c.cities === 'object' ? c.cities : c;
      state.cities = {};
      for (const [name, v] of Object.entries(map || {})) {
        const pair = Array.isArray(v) ? v : [v.lat ?? v.latitude, v.lon ?? v.lng ?? v.longitude];
        if (pair && !isNaN(pair[0]) && !isNaN(pair[1])) state.cities[name] = [Number(pair[0]), Number(pair[1])];
      }
      state.citiesLower = Object.fromEntries(Object.entries(state.cities).map(([k, v]) => [k.toLowerCase(), v]));
    } catch { /* map degrades to nothing; tables still work */ }
  }

  let refreshing = null;
  function refreshData() {
    if (!refreshing) refreshing = doRefresh().finally(() => { refreshing = null; });
    return refreshing;
  }
  async function doRefresh() {
    const meta = $('#kpi-updated');
    const [k, s, v] = await Promise.allSettled([api('/api/kpis'), api('/api/shipments?limit=500'), api('/api/vehicles')]);
    if (k.status === 'fulfilled') state.kpis = k.value;
    if (s.status === 'fulfilled') state.shipments = Array.isArray(s.value) ? s.value : (s.value.shipments || []);
    if (v.status === 'fulfilled') state.vehicles = Array.isArray(v.value) ? v.value : (v.value.vehicles || []);
    const failed = [k, s, v].filter((r) => r.status === 'rejected');
    if (failed.length === 3) {
      meta.textContent = `data offline · ${failed[0].reason.status || 'net'}`;
      meta.className = 'head-meta is-error';
      if (!state.shipments.length) $('#ship-body').innerHTML = `<tr class="empty-row"><td colspan="4">Logistics data service unavailable (${esc(failed[0].reason.message)}). Retrying every 10s.</td></tr>`;
    } else {
      meta.textContent = `synced ${fmtClock(new Date())}`;
      meta.className = 'head-meta is-fresh';
      setTimeout(() => meta.classList.remove('is-fresh'), 1200);
      populateCityFilter();
      renderTable();
      renderKpis();
      drawLanes();
      drawCities();
      drawFleet();
      renderLegend();
    }
  }
  async function refreshSlow() {
    const [w, inv] = await Promise.allSettled([api('/api/warehouses'), api('/api/inventory?low_stock_only=true')]);
    if (w.status === 'fulfilled') state.warehouses = Array.isArray(w.value) ? w.value : (w.value.warehouses || []);
    if (inv.status === 'fulfilled') state.lowStock = Array.isArray(inv.value) ? inv.value : (inv.value.inventory || []);
    drawHubs();
    drawCities();
    renderKpis();
    renderLegend();
  }

  // ================================================================= MARKDOWN
  const NUM_CELL = /^[-+]?(₹|\$|INR\s?|Rs\.?\s?)?[\d,]+(\.\d+)?\s?(%|kg|km|h|hrs?|min|mins|d|days)?$/i;
  function inline(t) {
    const tokens = [];
    const keep = (html) => { tokens.push(html); return `\u0001${tokens.length - 1}\u0001`; };
    t = t.replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${c}</code>`));
    t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => keep(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`));
    t = t.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[\s(])__(?=\S)([\s\S]*?\S)__(?=$|[\s).,;:!?])/g, '$1<strong>$2</strong>');
    t = t.replace(/(^|[^*\w])\*(?=\S)([^*\n]*?\S)\*(?!\*)/g, '$1<em>$2</em>');
    t = t.replace(/(^|[^\w-])(SHP-\d{3,6})\b/g, '$1<button type="button" class="ref" data-ref="$2" title="Show $2 on the map">$2</button>');
    return t.replace(/\u0001(\d+)\u0001/g, (_, k) => tokens[+k]);
  }

  function md(src) {
    let s = esc(String(src ?? '').replace(/\r\n?/g, '\n'));
    const blocks = [];
    s = s.replace(/```[^\n]*\n([\s\S]*?)(?:```|$)/g, (_, code) => {
      blocks.push(`<pre><code>${code.replace(/\n$/, '')}</code></pre>`);
      return `\n\u0000${blocks.length - 1}\u0000\n`;
    });
    const lines = s.split('\n');
    const n = lines.length;
    const out = [];
    const isBlank = (l) => !l.trim();
    const isFence = (l) => /^\u0000\d+\u0000$/.test(l.trim());
    const isHeading = (l) => /^\s{0,3}#{1,6}\s+\S/.test(l);
    const isHr = (l) => /^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(l);
    const isList = (l) => /^\s*(?:[-*+•]|\d{1,3}[.)])\s+\S/.test(l);
    const isQuote = (l) => /^\s*&gt;/.test(l);
    const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
    const isTable = (k) => lines[k].includes('|') && k + 1 < n && isSep(lines[k + 1]);
    const startsBlock = (k) => isFence(lines[k]) || isHeading(lines[k]) || isHr(lines[k]) || isList(lines[k]) || isQuote(lines[k]) || isTable(k);
    const cells = (row) => {
      let r = row.trim();
      if (r.startsWith('|')) r = r.slice(1);
      if (r.endsWith('|')) r = r.slice(0, -1);
      return r.split('|').map((c) => c.trim());
    };

    let i = 0;
    while (i < n) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      if (isFence(line)) { out.push(blocks[Number(line.trim().slice(1, -1))]); i++; continue; }
      if (isHeading(line)) { out.push(`<h4 class="md-h">${inline(line.replace(/^\s*#+\s+/, '').replace(/\s*#+\s*$/, ''))}</h4>`); i++; continue; }
      if (isTable(i)) {
        const head = cells(lines[i]);
        i += 2;
        const rows = [];
        while (i < n && lines[i].includes('|') && !isBlank(lines[i])) { rows.push(cells(lines[i])); i++; }
        out.push(`<div class="table-scroll"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${
          rows.map((r) => `<tr>${head.map((_, j) => { const c = r[j] ?? ''; return `<td${NUM_CELL.test(c) ? ' class="num"' : ''}>${inline(c)}</td>`; }).join('')}</tr>`).join('')
        }</tbody></table></div>`);
        continue;
      }
      if (isHr(line)) { out.push('<hr>'); i++; continue; }
      if (isList(line)) {
        const ordered = /^\s*\d/.test(line);
        const start = ordered ? parseInt(line.trim(), 10) : 1;
        const items = [];
        while (i < n) {
          const l = lines[i];
          const m = l.match(/^(\s*)(?:[-*+•]|\d{1,3}[.)])\s+(.*)$/);
          if (m && !isHr(l)) { items.push({ indent: m[1].length, text: m[2] }); i++; continue; }
          if (!isBlank(l) && /^\s{2,}\S/.test(l) && items.length) { items[items.length - 1].text += ` ${l.trim()}`; i++; continue; }
          if (isBlank(l) && i + 1 < n && isList(lines[i + 1])) { i++; continue; }
          break;
        }
        const base = Math.min(...items.map((it) => it.indent));
        const tag = ordered ? 'ol' : 'ul';
        out.push(`<${tag}${ordered && start > 1 ? ` start="${start}"` : ''}>${items.map((it) => `<li${it.indent > base ? ' class="nested"' : ''}>${inline(it.text)}</li>`).join('')}</${tag}>`);
        continue;
      }
      if (isQuote(line)) {
        const q = [];
        while (i < n && isQuote(lines[i])) { q.push(lines[i].replace(/^\s*&gt;\s?/, '')); i++; }
        out.push(`<blockquote>${inline(q.join('\n')).replace(/\n/g, '<br>')}</blockquote>`);
        continue;
      }
      const para = [line];
      i++;
      while (i < n && !isBlank(lines[i]) && !startsBlock(i)) { para.push(lines[i]); i++; }
      out.push(`<p>${inline(para.join('\n')).replace(/\n/g, '<br>')}</p>`);
    }
    return out.join('');
  }

  // ================================================================= CHAT
  const messagesEl = () => $('#messages');
  function nearBottom() { const m = messagesEl(); return m.scrollHeight - m.scrollTop - m.clientHeight < 140; }
  function scrollDown(force = false) { const m = messagesEl(); if (force || nearBottom()) m.scrollTop = m.scrollHeight; }

  function newSession() {
    if (state.abort) state.abort.abort();
    const rnd = (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : Math.random().toString(36).slice(2) + Date.now().toString(36)).slice(0, 10);
    state.session = `ops-${rnd}`;
    $('#session-id').textContent = state.session;
    messagesEl().innerHTML = '';
    addWelcome();
  }

  function addWelcome() {
    const el = document.createElement('article');
    el.className = 'msg msg-agent is-welcome';
    el.innerHTML = `
      <div class="agent-head"><span class="agent-badge" aria-hidden="true">CM</span><span class="agent-name">CargoMind</span><span class="agent-via">ops agent online</span></div>
      <div class="md">${md(
        'I run a team of specialist agents: **tracking**, **dispatch planning** with a critic loop, **weather and route risk**, ' +
        '**inventory**, **carrier quotes**, **customer comms** and **proof-of-delivery vision**.\n\n' +
        '- Every tool call is guarded by self-healing. Raise **Chaos Monkey** to watch retries, fallbacks and breakers in the journal below.\n' +
        '- Rate any answer. Your corrections become **lessons learned** that I apply on the next run.\n' +
        '- Attach or paste a delivery photo and I will inspect it for damage.')}</div>`;
    messagesEl().append(el);
  }

  function appendUser(text, att) {
    const el = document.createElement('div');
    el.className = 'msg msg-user';
    const img = att && att.ref ? `<img src="/api/uploads/${encodeURIComponent(att.ref)}" alt="Attached delivery photo">` : '';
    el.innerHTML = `<div class="bubble">${img}${esc(text)}</div>`;
    messagesEl().append(el);
    scrollDown(true);
  }

  async function streamChat(body, onEvent, signal) {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) {
      let msg = `HTTP ${res.status}`;
      try { const j = await res.json(); msg = j.error || j.detail || msg; if (typeof msg !== 'string') msg = JSON.stringify(msg); } catch { /* ignore */ }
      throw new Error(msg);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const data = frame.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).replace(/^ /, '')).join('\n');
        if (!data) continue; // keep-alive comment
        let ev;
        try { ev = JSON.parse(data); } catch { continue; }
        onEvent(ev);
      }
    }
  }

  function renderArgs(args) {
    if (args === undefined || args === null) return '';
    if (typeof args !== 'object') return `<span class="tool-args">${esc(compact(args, 200))}</span>`;
    const parts = Object.entries(args).filter(([k]) => k !== 'tool_context').map(([k, v]) =>
      `<span class="k">${esc(k)}</span>=${esc(compact(typeof v === 'string' ? v : v, 90))}`);
    return parts.length ? `<span class="tool-args">${parts.join(' · ')}</span>` : '<span class="tool-args"><span class="k">(no args)</span></span>';
  }

  function createReply() {
    const el = document.createElement('article');
    el.className = 'msg msg-agent';
    el.setAttribute('aria-busy', 'true');
    el.innerHTML = `
      <div class="agent-head"><span class="agent-badge" aria-hidden="true">CM</span><span class="agent-name">CargoMind</span><span class="agent-via"></span><span class="agent-time">${esc(fmtHM(new Date()))}</span></div>
      <div class="live-status"><span class="spinner" aria-hidden="true"></span><span class="live-text">Routing your request to the right specialist…</span></div>
      <div class="md body"></div>
      <div class="err-text" hidden></div>
      <div class="model-chips"></div>
      <details class="trace" open hidden>
        <summary><span>Agent trace</span><span class="trace-stat t-sum"></span><span class="trace-stat heal t-heal"></span><span class="trace-stat t-time"></span></summary>
        <ol class="steps"></ol>
      </details>
      <div class="feedback" hidden></div>`;
    messagesEl().append(el);
    scrollDown(true);

    const r = {
      el,
      body: $('.body', el), steps: $('.steps', el), trace: $('.trace', el), live: $('.live-text', el),
      segments: [], agents: new Set(), tools: 0, heals: 0, models: new Set(), pending: [],
      lastAuthor: null, textAuthor: null, t0: performance.now(), userToggled: false, finished: false,
      errored: false, rafPending: false, mentioned: new Set(),
    };
    $('summary', el).addEventListener('click', () => { r.userToggled = true; });

    const since = () => `+${((performance.now() - r.t0) / 1000).toFixed(1)}s`;
    const addStep = (cls, html) => {
      const li = document.createElement('li');
      li.className = `step ${cls}`;
      li.innerHTML = html;
      r.steps.append(li);
      r.trace.hidden = false;
      updateSummary();
      scrollDown();
      return li;
    };
    const updateSummary = () => {
      const parts = [];
      if (r.agents.size) parts.push(`${r.agents.size} agent${r.agents.size > 1 ? 's' : ''}`);
      if (r.tools) parts.push(`${r.tools} tool call${r.tools > 1 ? 's' : ''}`);
      $('.t-sum', el).textContent = parts.join(' · ');
      $('.t-heal', el).textContent = r.heals ? `${r.heals} self-heal${r.heals > 1 ? 's' : ''}` : '';
    };
    const setLive = (html) => { r.live.innerHTML = html; };
    const noteAgent = (author, explicit, extra) => {
      if (!author || author === 'user' || author === 'system') return;
      if (author === r.lastAuthor) return;
      const first = r.lastAuthor === null;
      r.lastAuthor = author;
      r.agents.add(author);
      addStep('step-agent', `<div class="step-row"><span class="who">${esc(agentLabel(author))}</span><span class="what">${first ? 'picked up the request' : 'took over'}${extra ? ` · ${esc(extra)}` : ''}</span><span class="step-t">${since()}</span></div>`);
      setLive(`<b>${esc(agentLabel(author))}</b> is on it…`);
    };
    const bodyHtml = () => {
      const authors = new Set(r.segments.map((s) => s.author));
      const multi = authors.size > 1;
      return r.segments.map((s) => (multi ? `<div class="seg-author">${esc(agentLabel(s.author))}</div>` : '') + md(s.text)).join('');
    };
    r.bodyHtml = bodyHtml;
    const renderBody = () => {
      if (r.rafPending) return;
      r.rafPending = true;
      requestAnimationFrame(() => {
        r.rafPending = false;
        r.body.innerHTML = bodyHtml() + (r.finished ? '' : '<span class="caret" aria-hidden="true"></span>');
        scrollDown();
      });
    };
    r.interim = [];
    const addModel = (m) => { if (m && typeof m === 'string') r.models.add(m); };

    r.handle = (ev) => {
      if (!ev || typeof ev !== 'object') return;
      const type = ev.type;
      addModel(ev.served_by);
      if (ev.author && !['healing', 'done', 'error'].includes(type)) noteAgent(ev.author, type === 'agent', type === 'agent' ? ev.text : '');

      if (type === 'tool_call') {
        r.tools++;
        const tool = ev.tool || ev.name || 'tool';
        JSON.stringify(ev.args || {}).replace(/SHP-\d{3,6}/g, (m) => { r.mentioned.add(m); return m; });
        const li = addStep('step-tool', `<div class="step-row"><span class="tool-name">${esc(tool)}</span><span class="tool-by">${ev.author ? `by ${esc(agentLabel(ev.author))}` : ''}</span><span class="step-t">${since()}</span></div>${renderArgs(ev.args)}<div class="tool-result is-pending">running…</div>`);
        r.pending.push({ tool, li });
        setLive(`<b>${esc(agentLabel(ev.author || r.lastAuthor))}</b> → <code>${esc(tool)}</code>`);
      } else if (type === 'tool_result') {
        const tool = ev.tool || ev.name || 'tool';
        let idx = -1;
        for (let k = r.pending.length - 1; k >= 0; k--) if (r.pending[k].tool === tool) { idx = k; break; }
        let li;
        if (idx >= 0) { li = r.pending[idx].li; r.pending.splice(idx, 1); }
        else li = addStep('step-tool', `<div class="step-row"><span class="tool-name">${esc(tool)}</span><span class="step-t">${since()}</span></div><div class="tool-result"></div>`);
        fillResult(li, ev.result !== undefined ? ev.result : ev.response);
        updateSummary();
        setLive(`<b>${esc(agentLabel(ev.author || r.lastAuthor))}</b> is reading <code>${esc(tool)}</code>…`);
      } else if (type === 'healing') {
        r.heals++;
        const e = ev.event && typeof ev.event === 'object' ? ev.event : ev;
        const kind = normKind(e.kind);
        const tail = [e.action && `→ ${e.action}`, e.outcome && `(${e.outcome})`].filter(Boolean).join(' ');
        addStep('step-heal', `<div class="step-row"><span class="kind k-${esc(kind)}">${esc(kindLabel(e))}</span><span class="heal-comp">${esc(e.component || '')}</span><span class="step-t">${since()}</span></div><div class="heal-detail">${esc(e.detail || ev.text || '')} ${esc(tail)}</div>`);
        setLive(`<b style="color:var(--amber)">Self-healing</b> · ${esc(kindLabel(e))} on <code>${esc(e.component || '?')}</code>`);
        if (e.kind) addJournal(e, true); // keeps the drawer in sync even if its SSE is down (dedup by id)
      } else if (type === 'text') {
        const t = ev.text ?? ev.content ?? '';
        if (t) {
          const author = ev.author || r.lastAuthor || 'agent';
          const last = r.segments[r.segments.length - 1];
          const isFinal = ev.final === undefined || ev.final === null ? true : !!ev.final;
          if (!ev.partial && !isFinal) {
            // interim narration ("let me check the weather…") belongs in the trace, not the answer
            r.interim.push({ author, text: t });
            addStep('step-note', `<div class="step-row"><span class="who-note">${esc(agentLabel(author))} noted</span><span class="step-t">${since()}</span></div><div class="note-text">${esc(compact(t, 320))}</div>`);
          } else if (ev.partial) {
            if (last && last.author === author && last.partial) last.text += t;
            else r.segments.push({ author, text: t, partial: true });
          } else if (last && last.author === author && last.partial) {
            last.text = t; last.partial = false;
          } else if (!(last && last.author === author && last.text === t)) {
            r.segments.push({ author, text: t, partial: false });
          }
          if (ev.partial || isFinal) {
            r.textAuthor = author;
            setLive(`<b>${esc(agentLabel(author))}</b> is writing…`);
            renderBody();
          }
        }
      } else if (type === 'done') {
        if (ev.text && !r.segments.length) { r.segments.push({ author: ev.author, text: ev.text }); renderBody(); }
      } else if (type === 'error') {
        r.errored = true;
        const box = $('.err-text', el);
        box.hidden = false;
        box.textContent = ev.text || ev.error || ev.message || 'Something went wrong.';
        if (!ev.soft) el.classList.add('is-error');
      }
    };

    r.finish = () => {
      r.finished = true;
      el.removeAttribute('aria-busy');
      const ls = $('.live-status', el);
      if (ls) ls.remove();
      for (const p of r.pending) {
        const box = $('.tool-result', p.li);
        if (box) { box.textContent = 'no result reported'; box.classList.remove('is-pending'); }
      }
      if (!r.segments.length && r.interim.length) r.segments.push({ ...r.interim[r.interim.length - 1], partial: false });
      const text = r.segments.map((s) => s.text).join('\n\n');
      r.body.innerHTML = r.bodyHtml();
      if (!text && !r.errored) r.body.innerHTML = '<p style="color:var(--dim)">No reply text was produced.</p>';
      r.ok = !!text && !r.errored;
      r.text = text;
      if (r.models.size) tallyModels(r);
      const secs = ((performance.now() - r.t0) / 1000).toFixed(1);
      $('.t-time', el).textContent = `${secs}s`;
      updateSummary();
      if (!r.userToggled) r.trace.open = false;

      const via = r.textAuthor && !isRootAuthor(r.textAuthor) ? `via ${agentLabel(r.textAuthor)}` : '';
      $('.agent-via', el).textContent = via;
      const chips = [...r.models].map((m) => {
        const cls = /offline|rule/i.test(m) ? 'is-offline' : /claude|anthropic/i.test(m) ? 'is-claude' : '';
        return `<span class="model-chip ${cls}" title="Model that served this reply">${esc(m)}</span>`;
      });
      if (r.heals) chips.push(`<span class="model-chip is-healed" title="Self-healing events during this reply">self-healed ×${r.heals}</span>`);
      $('.model-chips', el).innerHTML = chips.join('');

      if (text) buildFeedback(r, text);
      text.replace(/SHP-\d{3,6}/g, (m) => { r.mentioned.add(m); return m; });
      const ids = [...r.mentioned].filter((id) => state.lanes.has(id)).slice(0, 4);
      if (ids.length) setTimeout(() => focusMany(ids), 400);
      scrollDown();
    };
    return r;
  }

  function fillResult(li, res) {
    const box = $('.tool-result', li);
    if (!box) return;
    box.classList.remove('is-pending');
    const isErr = res && typeof res === 'object' && (res.status === 'error' || (res.error && res.status !== 'ok'));
    li.classList.add(isErr ? 'is-err' : 'is-ok');
    box.classList.toggle('is-err', !!isErr);
    const flags = [];
    if (res && typeof res === 'object') {
      if (res.degraded) flags.push('<span class="flag flag-degraded" title="Served by a fallback provider">degraded</span>');
      if (res.source) flags.push(`<span class="flag flag-src" title="Data source">${esc(compact(res.source, 40))}</span>`);
    }
    if (flags.length) $('.step-row', li).insertAdjacentHTML('beforeend', flags.join(''));
    const short = compact(res, 360);
    box.textContent = short;
    if (short.length > 150 || (typeof res === 'object' && res !== null)) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'result-toggle';
      btn.textContent = 'expand';
      btn.setAttribute('aria-expanded', 'false');
      btn.addEventListener('click', () => {
        const open = !box.classList.contains('is-open');
        box.classList.toggle('is-open', open);
        box.textContent = open ? prettyJson(res) : short;
        btn.textContent = open ? 'collapse' : 'expand';
        btn.setAttribute('aria-expanded', String(open));
      });
      box.after(btn);
    }
  }

  // ---------------------------------------------------------------- feedback
  function buildFeedback(r, text) {
    const fb = $('.feedback', r.el);
    fb.hidden = false;
    fb.innerHTML = `<span class="feedback-q">Was this useful?</span>
      <button type="button" class="fb-btn up" aria-pressed="false" aria-label="Helpful answer" title="Helpful">${ICON.up}</button>
      <button type="button" class="fb-btn down" aria-pressed="false" aria-label="Unhelpful answer" title="Needs work">${ICON.down}</button>`;
    const up = $('.up', fb); const down = $('.down', fb);
    const open = (rating) => {
      up.setAttribute('aria-pressed', String(rating > 0));
      down.setAttribute('aria-pressed', String(rating < 0));
      let form = $('.fb-form', fb);
      if (!form) {
        form = document.createElement('form');
        form.className = 'fb-form';
        form.innerHTML = `<label class="sr-only" for="fb-${r.t0}">Feedback comment</label>
          <input id="fb-${r.t0}" type="text" maxlength="500" autocomplete="off">
          <button type="submit" class="small-btn">Send</button>
          <button type="button" class="small-btn secondary">Cancel</button>`;
        fb.append(form);
        $('.secondary', form).addEventListener('click', () => {
          form.remove();
          up.setAttribute('aria-pressed', 'false'); down.setAttribute('aria-pressed', 'false');
        });
        form.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('.secondary', form).click(); });
        form.addEventListener('submit', (e) => {
          e.preventDefault();
          submitFeedback(r, fb, Number(form.dataset.rating), $('input', form).value.trim(), text);
        });
      }
      form.dataset.rating = String(rating);
      const input = $('input', form);
      input.placeholder = rating > 0 ? 'What made this good? (optional)' : 'What should CargoMind do differently? (optional)';
      input.focus();
    };
    up.addEventListener('click', () => open(1));
    down.addEventListener('click', () => open(-1));
  }

  async function submitFeedback(r, fb, rating, comment, text) {
    const send = $('.fb-form .small-btn', fb);
    if (send) { send.disabled = true; send.textContent = 'Saving…'; }
    try {
      await api('/api/feedback', {
        json: { session_id: state.session, rating, comment, message: text.slice(0, 4000), agent: r.textAuthor || r.lastAuthor || '' },
      });
      fb.innerHTML = `<span class="fb-done">${ICON.check}${rating > 0 ? 'Rated helpful' : 'Rated needs work'}${comment ? ' · saved as a lesson for future runs' : ' · logged'}</span>`;
      toast(comment ? 'Feedback stored. CargoMind will apply this lesson next time.' : 'Rating logged. Thanks!', 'ok');
      loadLessons(true);
    } catch (e) {
      if (send) { send.disabled = false; send.textContent = 'Send'; }
      toast(`Feedback not saved: ${e.message}`, 'err');
    }
  }

  // ---------------------------------------------------------------- send
  function withLanguage(text) {
    if (!state.lang || !COMMS_RE.test(text) || HAS_LANG_RE.test(text)) return text;
    return `${text.replace(/[\s.?!]+$/, '')} in ${state.lang}`;
  }

  /** Sends one operator turn. Resolves to {ok, text} once the reply has finished streaming. */
  async function sendMessage(raw) {
    if (state.streaming) { toast('The agent is still working on the previous request.', 'warn'); return { ok: false }; }
    const att = state.attachment;
    if (att && !att.ref && !att.failed) { toast('Photo is still uploading…', 'warn'); return { ok: false }; }
    let text = String(raw ?? '').trim();
    const ref = att && att.ref ? att : null;
    if (!text && !ref) return { ok: false };
    if (!text && ref) text = 'Inspect this proof-of-delivery photo for damage and recommend next steps.';
    text = withLanguage(text);
    appendUser(text, ref);
    clearAttachment();
    const input = $('#chat-input');
    input.value = '';
    autosize(input);

    const reply = createReply();
    const ctrl = new AbortController();
    state.abort = ctrl;
    setStreaming(true);
    try {
      await streamChat({ session_id: state.session, message: text, image_ref: ref ? ref.ref : null }, (ev) => reply.handle(ev), ctrl.signal);
    } catch (e) {
      if (e.name === 'AbortError') reply.handle({ type: 'error', text: 'Stopped by operator.', soft: true });
      else reply.handle({ type: 'error', text: `Connection problem: ${e.message}` });
    } finally {
      reply.finish();
      state.abort = null;
      setStreaming(false);
      refreshData();
      loadHealing();
      loadAlerts();
    }
    return { ok: !!reply.ok, text: reply.text || '' };
  }

  // ---------------------------------------------------------------- models served tally
  function tallyModels(r) {
    const m = state.models;
    m.replies++;
    for (const name of r.models) m.counts[name] = (m.counts[name] || 0) + 1;
    m.recent.unshift({ at: new Date(), agent: r.textAuthor || r.lastAuthor, models: [...r.models], heals: r.heals });
    m.recent.length = Math.min(m.recent.length, 8);
    const btn = $('#models-btn');
    $('#models-total').textContent = String(m.replies);
    btn.classList.remove('bump'); void btn.offsetWidth; btn.classList.add('bump');
    if (!$('#models-pop').hidden) renderModelsPop();
  }
  const modelCls = (name) => (/offline|rule/i.test(name) ? 'is-offline' : /claude|anthropic/i.test(name) ? 'is-claude' : '');

  function renderModelsPop() {
    const m = state.models;
    const entries = Object.entries(m.counts).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...entries.map(([, n]) => n));
    $('#models-sub').textContent = `${m.replies} repl${m.replies === 1 ? 'y' : 'ies'} · ${entries.length} model${entries.length === 1 ? '' : 's'}`;
    $('#models-tally').innerHTML = entries.length
      ? entries.map(([name, n]) => `<li class="${modelCls(name)}"><span class="m-name" title="${esc(name)}">${esc(name)}</span><span class="m-bar"><i style="width:${(100 * n) / max}%"></i></span><b>${n}</b></li>`).join('')
      : '<li class="mp-empty">No replies yet this session. Ask the agent something and every model that served it is tallied here, including fallbacks.</li>';
    $('#models-recent').innerHTML = m.recent.map((x) =>
      `<li><time>${esc(fmtHM(x.at))}</time><span class="who">${esc(agentLabel(x.agent))}${x.heals ? ` · <span style="color:var(--amber)">healed ×${x.heals}</span>` : ''}</span>
        <span class="chips">${x.models.map((n) => `<span class="model-chip ${modelCls(n)}">${esc(n)}</span>`).join('')}</span></li>`).join('') ||
      '<li class="mp-empty">—</li>';
  }
  function toggleModelsPop(force) {
    const pop = $('#models-pop');
    const btn = $('#models-btn');
    const open = force === undefined ? pop.hidden : force;
    if (!open) { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); return; }
    renderModelsPop();
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    const rect = btn.getBoundingClientRect();
    const w = pop.offsetWidth;
    pop.style.top = `${Math.round(rect.bottom + 8)}px`;
    pop.style.left = `${Math.round(Math.max(8, Math.min(rect.right - w, window.innerWidth - w - 8)))}px`;
  }

  // ---------------------------------------------------------------- disruption autopilot
  function currentScenario() { return SCENARIOS[Number($('#ap-scenario').value) || 0]; }
  function renderScenarioGlyph() { $('#ap-glyph').innerHTML = GLYPH[currentScenario().glyph] || ''; }

  function setIncident(sc, st) {
    const box = $('#incident');
    clearInterval(setIncident._timer);
    if (!sc) {
      box.dataset.state = 'idle';
      box.innerHTML = '<span class="inc-idle"><i></i>No active incidents · Watchtower scans weather, SLAs and stock every 60s</span>';
      return;
    }
    const t0 = state.incident && state.incident.sc === sc && state.incident.t0 ? state.incident.t0 : Date.now();
    state.incident = { sc, state: st, t0 };
    box.dataset.state = st;
    const stateText = { running: 'autopilot running', ready: 'autopilot plan ready · see agent reply', failed: 'autopilot hit a problem · see agent reply' }[st];
    box.innerHTML = `<span class="inc-tag">Incident</span>
      <span class="inc-text"><b>${esc(sc.event)}</b> · ${esc(sc.city)} · severity <b>high</b></span>
      <span class="inc-state">${st === 'running' ? '<span class="spinner" aria-hidden="true"></span>' : ''}<span class="inc-state-text">${esc(stateText)}</span><span class="inc-clock"></span></span>
      <button type="button" class="inc-close" aria-label="Dismiss incident" title="Dismiss incident">${ICON.x}</button>`;
    $('.inc-close', box).addEventListener('click', clearIncident);
    const clock = $('.inc-clock', box);
    const tick = () => { const s = Math.round((Date.now() - t0) / 1000); clock.textContent = ` · ${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };
    tick();
    if (st === 'running') setIncident._timer = setInterval(tick, 1000);
  }

  function showIncidentOnMap(sc) {
    if (!state.map) return;
    clearIncidentMap();
    const c = coord(sc.city) || sc.coord;
    const red = STATUS_COLOR.delayed;
    const zone = L.circle(c, { radius: sc.radiusKm * 1000, color: red, weight: 1.5, dashArray: '4 6', fillColor: red, fillOpacity: 0.08, className: 'incident-zone', interactive: false }).addTo(state.map);
    const ring = L.marker(c, {
      icon: L.divIcon({ className: 'incident-icon', html: `<div class="incident-ring"><i></i><i></i><i></i><span>${esc(sc.event)}</span></div>`, iconSize: [0, 0] }),
      interactive: false, keyboard: false, zIndexOffset: 2000,
    }).addTo(state.map);
    state.incidentLayers = [zone, ring];
    state.map.flyTo(c, 6.4, { duration: 1.1 });
    const city = sc.city.toLowerCase();
    const hit = state.shipments.filter((s) => [s.origin_city, s.destination_city, s.last_city].some((x) => String(x || '').toLowerCase() === city)).map((s) => s.id);
    if (hit.length) setTimeout(() => setHot(hit, 0), 600);
  }
  function clearIncidentMap() {
    for (const l of state.incidentLayers || []) l.remove();
    state.incidentLayers = [];
  }
  function clearIncident() {
    clearIncidentMap();
    state.incident = null;
    setIncident(null);
    setHot(state.selected ? [state.selected] : [], 0);
  }

  async function triggerAutopilot() {
    if (state.streaming) { toast('Wait for the current agent run to finish, or press Stop.', 'warn'); return; }
    const sc = currentScenario();
    state.incident = null;
    showIncidentOnMap(sc);
    setIncident(sc, 'running');
    $('#ap-trigger').disabled = true;
    try {
      const res = await sendMessage(autopilotPrompt(sc));
      if (state.incident && state.incident.sc === sc) setIncident(sc, res && res.ok ? 'ready' : 'failed');
    } finally {
      $('#ap-trigger').disabled = false;
    }
  }

  // ---------------------------------------------------------------- watchtower alerts
  const SEV_RANK = { critical: 0, warning: 1, info: 2 };
  function normSev(s) {
    const v = String(s || '').toLowerCase();
    if (/crit|high|severe|red/.test(v)) return 'critical';
    if (/warn|med|amber|elevated/.test(v)) return 'warning';
    return 'info';
  }
  const alertKey = (a) => String(a.id ?? `${a.kind}|${a.title}`);

  function renderAlerts(list, { announce = false } = {}) {
    const box = $('#alerts');
    const alerts = (Array.isArray(list) ? list : []).filter((a) => a && typeof a === 'object')
      .map((a) => ({ ...a, sev: normSev(a.severity) }))
      .sort((a, b) => SEV_RANK[a.sev] - SEV_RANK[b.sev]);
    const fresh = alerts.filter((a) => !state.alerts.seen.has(alertKey(a)));
    if (!alerts.length) {
      box.innerHTML = '<li class="quiet"><i></i>All quiet. Watchtower is watching weather, SLAs, stock and fleet.</li>';
    } else {
      box.innerHTML = alerts.map((a) => {
        const isNew = state.alerts.loaded && !state.alerts.seen.has(alertKey(a));
        const prompt = a.suggested_prompt || `Investigate: ${a.title || a.kind}`;
        return `<li><button type="button" class="alert sev-${a.sev}${isNew ? ' is-new' : ''}" data-prompt="${esc(prompt)}" title="${esc(`${a.detail || a.title || ''}\n\nClick to ask: ${prompt}`)}">
          <span class="alert-sev">${esc(a.sev)}</span>
          <span class="alert-main"><b>${esc(a.title || pretty(a.kind) || 'Alert')}</b><span>${esc(a.detail || a.kind || '')}</span></span>
          <span class="alert-go" aria-hidden="true">Ask →</span></button></li>`;
      }).join('');
    }
    if (announce && state.alerts.loaded) {
      const loud = fresh.filter((a) => a.sev !== 'info');
      if (loud.length) toast(`Watchtower: ${loud[0].title || loud[0].kind}${loud.length > 1 ? ` (+${loud.length - 1} more)` : ''}`, loud[0].sev === 'critical' ? 'err' : 'warn', 6000);
    }
    for (const a of alerts) state.alerts.seen.add(alertKey(a));
    state.alerts.loaded = true;
    const crit = alerts.filter((a) => a.sev === 'critical').length;
    const meta = $('#alerts-meta');
    meta.textContent = alerts.length ? `${alerts.length} active${crit ? ` · ${crit} critical` : ''} · ${fmtHM(new Date())}` : `clear · ${fmtHM(new Date())}`;
    meta.className = `head-meta${crit ? ' is-error' : ''}`;
  }

  async function loadAlerts() {
    try {
      const r = await api('/api/alerts');
      renderAlerts(Array.isArray(r) ? r : r.alerts, { announce: true });
    } catch (e) {
      if (!state.alerts.loaded) {
        $('#alerts').innerHTML = `<li class="quiet" style="color:var(--faint)"><i style="background:var(--faint)"></i>Watchtower offline (${esc(e.message)}). Retrying.</li>`;
        $('#alerts-meta').textContent = 'offline';
      }
    }
  }

  async function scanNow() {
    const btn = $('#scan-btn');
    btn.disabled = true;
    btn.textContent = 'Scanning…';
    try {
      const r = await api('/api/alerts/scan', { method: 'POST' });
      const n = (r.new || []).length;
      renderAlerts(r.active || [], { announce: false });
      toast(n ? `Watchtower found ${n} new risk${n > 1 ? 's' : ''}.` : 'Scan complete: no new risks.', n ? 'warn' : 'ok');
    } catch (e) {
      toast(`Scan failed: ${e.message}`, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Scan now';
    }
  }

  // ---------------------------------------------------------------- kill MCP
  async function killMcp() {
    const btn = $('#kill-mcp');
    btn.classList.remove('is-firing'); void btn.offsetWidth; btn.classList.add('is-firing');
    try {
      const r = await api('/api/chaos/mcp', { json: { down: true, times: 3 } });
      toast(`Injected ${r.injected ?? 3} faults into MCP tool calls. Ask the agent anything and watch self-healing recover.`, 'warn', 6000);
      setDrawer(true);
    } catch (e) {
      toast(`Could not inject faults: ${e.message}`, 'err');
    }
  }

  function setStreaming(on) {
    state.streaming = on;
    const btn = $('#send-btn');
    btn.classList.toggle('is-stop', on);
    $('.send-label', btn).textContent = on ? 'Stop' : 'Send';
    btn.setAttribute('aria-label', on ? 'Stop the agent' : 'Send message');
    document.querySelectorAll('.quick-chip').forEach((c) => { c.disabled = on; });
  }

  function autosize(t) {
    t.style.height = 'auto';
    t.style.height = `${Math.min(t.scrollHeight, 140)}px`;
  }

  // ---------------------------------------------------------------- attachments
  function renderAttachment(status, cls = '') {
    const box = $('#attach-preview');
    const a = state.attachment;
    if (!a) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML = `<img src="${a.url}" alt=""><span class="att-name">${ICON.camera}${esc(a.name || 'photo')}</span><span class="att-state ${cls}">${esc(status)}</span>
      <button type="button" class="icon-btn" aria-label="Remove photo" title="Remove photo">${ICON.x}</button>`;
    $('button', box).addEventListener('click', () => clearAttachment());
  }
  function clearAttachment() {
    if (state.attachment && state.attachment.url) setTimeout(((u) => () => URL.revokeObjectURL(u))(state.attachment.url), 5000);
    state.attachment = null;
    renderAttachment();
  }
  async function attachFile(file) {
    if (!file) return;
    if (!/^image\//.test(file.type)) { toast('Only image files can be inspected (PNG, JPEG, WebP, GIF).', 'err'); return; }
    if (file.size > 8 * 1024 * 1024) { toast('That image is larger than 8 MB.', 'err'); return; }
    clearAttachment();
    const a = { file, url: URL.createObjectURL(file), ref: null, name: file.name || 'pasted-photo.png', failed: false };
    state.attachment = a;
    renderAttachment('uploading…');
    const fd = new FormData();
    fd.append('file', file, a.name);
    try {
      const res = await api('/api/upload', { method: 'POST', body: fd });
      if (state.attachment !== a) return;
      a.ref = res.image_ref;
      renderAttachment('ready to send', 'ok');
      $('#chat-input').focus();
    } catch (e) {
      if (state.attachment !== a) return;
      a.failed = true;
      renderAttachment(e.message, 'err');
      toast(`Upload failed: ${e.message}`, 'err');
    }
  }

  // ================================================================= HEALING JOURNAL
  function normKind(k) {
    const s = String(k || '').toLowerCase().replace(/-/g, '_');
    if (s.startsWith('breaker')) return 'breaker';
    if (s === 'argrepair' || s === 'repair') return 'arg_repair';
    if (s === 'recover' || s === 'healed') return 'recovered';
    return KIND_ORDER.includes(s) ? s : (s || 'fault');
  }
  function kindLabel(e) {
    const k = String(e.kind || '').toLowerCase();
    if (k === 'breaker_open') return 'breaker open';
    if (k === 'breaker_close' || k === 'breaker_closed') return 'breaker closed';
    if (k === 'breaker_half_open') return 'breaker half-open';
    return KIND_LABEL[normKind(k)] || k.replace(/_/g, ' ');
  }
  const evTime = (e) => e.ts ?? e.time ?? e.timestamp ?? e.created_at ?? e.at;
  const jKey = (e) => (e.id !== undefined && e.id !== null ? `id:${e.id}` : `${evTime(e) ?? ''}|${e.kind}|${e.component}|${e.detail}`);

  function addJournal(e, live) {
    if (!e || typeof e !== 'object' || !e.kind) return;
    const key = jKey(e);
    if (state.heal.seen.has(key)) return;
    state.heal.seen.add(key);
    const list = $('#journal');
    const empty = $('.journal-empty', list);
    if (empty) empty.remove();
    const k = normKind(e.kind);
    const li = document.createElement('li');
    li.className = `jrow${live ? ' is-new' : ''}`;
    const out = [e.action, e.outcome].filter(Boolean);
    li.innerHTML = `<span class="jtime">${esc(fmtClock(toDate(evTime(e))) || fmtClock(new Date()))}</span>
      <span class="kind k-${esc(k)}">${esc(kindLabel(e))}</span>
      <span class="jcomp" title="${esc(e.component || '')}">${esc(e.component || '—')}</span>
      <span class="jdetail">${esc(e.detail || '')}${out.length ? ` <span class="jout">→ ${esc(e.action || '')}${e.outcome ? ` <b>${esc(e.outcome)}</b>` : ''}</span>` : ''}</span>`;
    list.prepend(li);
    while (list.children.length > 250) list.lastElementChild.remove();
    if (live) {
      state.heal.counts[k] = (state.heal.counts[k] || 0) + 1;
      renderCounters(k);
      const d = $('#drawer');
      d.classList.remove('is-ping');
      void d.offsetWidth;
      d.classList.add('is-ping');
    }
  }

  function journalEmpty() {
    const list = $('#journal');
    if (!list.children.length) {
      list.innerHTML = '<li class="journal-empty">No faults so far. Raise <b>Chaos Monkey</b> in the top bar, then ask the agent something: every injected failure, retry, fallback and recovery streams in here live.</li>';
    }
  }

  function renderCounters(bumped) {
    $('#heal-counters').innerHTML = KIND_ORDER.map((k) => {
      const n = state.heal.counts[k] || 0;
      return `<span class="kcount k-${k}${n ? '' : ' is-zero'}${k === bumped ? ' bump' : ''}" title="${n} ${esc(KIND_LABEL[k])} events"><b>${n}</b>${esc(KIND_LABEL[k])}</span>`;
    }).join('');
  }

  function countsFromStats(stats) {
    if (!stats || typeof stats !== 'object') return null;
    const src = [stats.by_kind, stats.counts, stats.kinds, stats].find((o) => o && typeof o === 'object' && Object.keys(o).some((k) => KIND_ORDER.includes(normKind(k)) && typeof o[k] === 'number'));
    if (!src) return null;
    const out = Object.fromEntries(KIND_ORDER.map((k) => [k, 0]));
    for (const [k, v] of Object.entries(src)) {
      const nk = normKind(k);
      if (typeof v === 'number' && KIND_ORDER.includes(nk)) out[nk] += v;
    }
    return out;
  }

  function renderRecovery(stats) {
    const el = $('#recovery');
    const rate = stats && typeof stats.recovery_rate === 'number' ? stats.recovery_rate : null;
    if (rate === null) { el.hidden = true; return; }
    el.hidden = false;
    el.textContent = `${Math.round(rate * 100)}% auto-recovered`;
    el.style.color = rate >= 0.8 ? 'var(--ok)' : rate >= 0.5 ? 'var(--amber)' : 'var(--bad)';
  }

  function renderBreakers(list) {
    const box = $('#breakers');
    const items = Array.isArray(list) ? list : [];
    const label = '<span class="breakers-label">Circuit breakers</span>';
    if (!items.length) { box.innerHTML = `${label}<span class="muted">all closed · none tripped yet</span>`; return; }
    box.innerHTML = label + items.map((b) => {
      const st = String(b.state || 'closed').toLowerCase().replace(/\s+/g, '_');
      const fails = b.failures ?? b.failure_count ?? b.consecutive_failures;
      return `<span class="breaker is-${esc(st)}" title="${esc(`${b.name}: ${st}${fails !== undefined ? `, ${fails} recent failures` : ''}`)}"><i></i>${esc(b.name)} · ${esc(st.replace('_', '-'))}</span>`;
    }).join('');
  }

  async function loadHealing(includeRecent = false) {
    try {
      const h = await api('/api/healing');
      if (includeRecent || !state.heal.loaded) {
        const recent = Array.isArray(h.recent) ? [...h.recent] : [];
        const timed = recent.every((e) => toDate(evTime(e)));
        if (timed) recent.sort((a, b) => toDate(evTime(a)) - toDate(evTime(b)));
        for (const e of recent) addJournal(e, false);
        state.heal.loaded = true;
        journalEmpty();
        if (!countsFromStats(h.stats)) {
          state.heal.counts = Object.fromEntries(KIND_ORDER.map((k) => [k, 0]));
          for (const e of recent) state.heal.counts[normKind(e.kind)] = (state.heal.counts[normKind(e.kind)] || 0) + 1;
        }
      }
      const c = countsFromStats(h.stats);
      if (c) state.heal.counts = c;
      renderCounters();
      renderRecovery(h.stats);
      renderBreakers(h.breakers);
      if (typeof h.chaos_rate === 'number') setChaosUI(Math.round(h.chaos_rate * 100), true);
    } catch (e) {
      if (!state.heal.loaded) {
        $('#journal').innerHTML = `<li class="journal-empty">Self-healing journal not available yet (${esc(e.message)}).</li>`;
        renderCounters();
        renderBreakers([]);
      }
    }
  }

  function setStream(st, text) {
    const el = $('#stream-state');
    el.dataset.state = st;
    $('span', el).textContent = text || st;
  }
  function connectHealing() {
    if (!window.EventSource) { setStream('offline', 'polling'); return; }
    if (state.heal.es) state.heal.es.close();
    const es = new EventSource('/api/healing/stream');
    state.heal.es = es;
    setStream('connecting', 'connecting');
    es.onopen = () => {
      setStream('live', 'live stream');
      if (state.heal.wasDown) loadHealing(true);
      state.heal.wasDown = false;
    };
    es.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      addJournal(ev, true);
    };
    es.onerror = () => {
      state.heal.wasDown = true;
      if (es.readyState === EventSource.CLOSED) {
        setStream('offline', 'reconnecting');
        setTimeout(connectHealing, 5000);
      } else {
        setStream('retrying', 'reconnecting');
      }
    };
  }

  // ---------------------------------------------------------------- lessons
  async function loadLessons(markNew = false) {
    try {
      const r = await api('/api/feedback/lessons');
      const raw = Array.isArray(r) ? r : (r.lessons || []);
      const lessons = raw.map((l) => (typeof l === 'string' ? l : l.text || l.lesson || l.comment || JSON.stringify(l)));
      const prev = new Set(state.lessons);
      state.lessons = lessons;
      $('#lessons-pill').textContent = `${lessons.length} lesson${lessons.length === 1 ? '' : 's'} learned`;
      $('#lessons').innerHTML = lessons.length
        ? lessons.map((l) => `<li class="${markNew && !prev.has(l) ? 'is-new' : ''}">${esc(l)}</li>`).join('')
        : '<li class="empty">No lessons yet. Rate an agent reply (thumbs up or down) and add a note: it is distilled into a lesson and injected into the agents\' instructions.</li>';
    } catch (e) {
      $('#lessons').innerHTML = `<li class="empty">Feedback store not available yet (${esc(e.message)}).</li>`;
    }
  }

  // ================================================================= DRAWER
  function setDrawer(open) {
    const d = $('#drawer');
    if ((d.dataset.open === 'true') === open) return;
    d.dataset.open = String(open);
    $('#drawer-toggle').setAttribute('aria-expanded', String(open));
    store.set('cm.drawer', open ? '1' : '0');
    setTimeout(() => state.map && state.map.invalidateSize(), 340);
  }

  // ================================================================= WIRING
  function wire() {
    // quick chips
    $('#quick').innerHTML = QUICK.map((q) => `<button type="button" class="quick-chip">${esc(q)}</button>`).join('') +
      '<button type="button" class="quick-chip" data-action="photo">Inspect a delivery photo</button>';
    $('#quick').addEventListener('click', (e) => {
      const b = e.target.closest('.quick-chip');
      if (!b || b.disabled) return;
      if (b.dataset.action === 'photo') { $('#file-input').click(); return; }
      sendMessage(b.textContent);
    });

    // composer
    const input = $('#chat-input');
    $('#composer').addEventListener('submit', (e) => {
      e.preventDefault();
      if (state.streaming) { if (state.abort) state.abort.abort(); return; }
      sendMessage(input.value);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!state.streaming) sendMessage(input.value);
      }
    });
    input.addEventListener('input', () => autosize(input));
    input.addEventListener('paste', (e) => {
      const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith('image/'));
      if (item) { e.preventDefault(); attachFile(item.getAsFile()); }
    });
    $('#attach-btn').addEventListener('click', () => $('#file-input').click());
    $('#file-input').addEventListener('change', (e) => { attachFile(e.target.files[0]); e.target.value = ''; });
    $('#new-session').addEventListener('click', () => { newSession(); toast('New agent session started.'); input.focus(); });

    // drag & drop photos onto the chat column
    const col = $('#chat-col'); const overlay = $('#drop-overlay');
    let depth = 0;
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
    col.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; overlay.hidden = false; });
    col.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
    col.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) overlay.hidden = true; });
    col.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault(); depth = 0; overlay.hidden = true;
      attachFile(e.dataTransfer.files[0]);
    });
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => e.preventDefault());

    // shipment references inside chat replies
    messagesEl().addEventListener('click', (e) => {
      const ref = e.target.closest('.ref');
      if (ref) selectShipment(ref.dataset.ref);
    });

    // filters
    $('#f-status').addEventListener('change', (e) => { state.filters.status = e.target.value; applyFilters(); renderLegend(); });
    $('#f-city').addEventListener('change', (e) => { state.filters.city = e.target.value; applyFilters(); });
    let qt = null;
    $('#f-search').addEventListener('input', (e) => { clearTimeout(qt); qt = setTimeout(() => { state.filters.q = e.target.value; applyFilters(); }, 120); });

    // table rows
    const body = $('#ship-body');
    body.addEventListener('click', (e) => { const tr = e.target.closest('tr[data-id]'); if (tr) selectShipment(tr.dataset.id); });
    body.addEventListener('keydown', (e) => {
      const tr = e.target.closest('tr[data-id]');
      if (!tr) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectShipment(tr.dataset.id); }
      if (e.key === 'ArrowDown' && tr.nextElementSibling) { e.preventDefault(); tr.nextElementSibling.focus(); }
      if (e.key === 'ArrowUp' && tr.previousElementSibling) { e.preventDefault(); tr.previousElementSibling.focus(); }
    });

    // KPI tiles
    $('#kpi-grid').addEventListener('click', (e) => { const t = e.target.closest('.kpi'); if (t) onKpiClick(t.dataset.kpi); });

    // legend filter buttons
    $('#map-legend').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-status]');
      if (!b) return;
      setStatusFilter(state.filters.status === b.dataset.status ? '' : b.dataset.status);
      renderLegend();
    });

    // chaos + kill MCP
    chaosRange().addEventListener('input', onChaosInput);
    $('#kill-mcp').addEventListener('click', killMcp);

    // disruption autopilot
    const sel = $('#ap-scenario');
    sel.innerHTML = SCENARIOS.map((sc, i) => `<option value="${i}">${esc(sc.event)} · ${esc(sc.city)}</option>`).join('');
    sel.addEventListener('change', renderScenarioGlyph);
    renderScenarioGlyph();
    $('#ap-trigger').addEventListener('click', triggerAutopilot);

    // watchtower
    $('#scan-btn').addEventListener('click', scanNow);
    $('#alerts').addEventListener('click', (e) => {
      const b = e.target.closest('.alert[data-prompt]');
      if (b) sendMessage(b.dataset.prompt);
    });

    // notice language
    $('#lang').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-lang]');
      if (!b) return;
      state.lang = b.dataset.lang;
      store.set('cm.lang', state.lang);
      $('#lang').querySelectorAll('button').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
      if (state.lang) toast(`Customer notices will be drafted in ${state.lang}.`);
    });
    const savedLang = store.get('cm.lang', '');
    const langBtn = $(`#lang button[data-lang="${CSS.escape(savedLang)}"]`);
    if (savedLang && langBtn) {
      state.lang = savedLang;
      $('#lang').querySelectorAll('button').forEach((x) => x.setAttribute('aria-checked', String(x === langBtn)));
    }

    // models popover
    $('#models-btn').addEventListener('click', (e) => { e.stopPropagation(); toggleModelsPop(); });
    $('#modes').addEventListener('click', (e) => { if (e.target.closest('.mode-pill')) { e.stopPropagation(); toggleModelsPop(true); } });
    document.addEventListener('click', (e) => {
      const pop = $('#models-pop');
      if (!pop.hidden && !pop.contains(e.target)) toggleModelsPop(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('#models-pop').hidden) { toggleModelsPop(false); $('#models-btn').focus(); }
    });
    window.addEventListener('resize', () => { if (!$('#models-pop').hidden) toggleModelsPop(true); });

    // drawer
    $('#drawer-toggle').addEventListener('click', () => setDrawer($('#drawer').dataset.open !== 'true'));

    // "/" focuses the chat box
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) {
        e.preventDefault();
        input.focus();
      }
    });
  }

  // ================================================================= BOOT
  async function boot() {
    buildKpiTiles();
    renderCounters();
    wire();
    tickClock();
    setInterval(tickClock, 1000);
    newSession();
    const wantOpen = store.get('cm.drawer');
    setDrawer(wantOpen === null ? window.innerWidth < 1100 : wantOpen === '1');

    initMap();
    loadHealth();
    await loadCities();
    await Promise.all([refreshSlow(), refreshData()]);
    loadHealing(true);
    connectHealing();
    loadLessons();
    loadAlerts();

    setInterval(() => { if (!document.hidden) refreshData(); }, 10000);
    setInterval(() => { if (!document.hidden) loadAlerts(); }, 15000);
    setInterval(() => { if (!document.hidden) refreshSlow(); }, 30000);
    setInterval(() => { if (!document.hidden) loadHealing(); }, 15000);
    setInterval(() => { if (!document.hidden) loadLessons(); }, 30000);
    setInterval(() => { if (!document.hidden) loadHealth(); }, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshData(); });
  }

  window.cargomind = { state, sendMessage, focusShipment, triggerAutopilot, md };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
