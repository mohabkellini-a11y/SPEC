/* Today view. Vanilla JS, no build step, no external requests.
 *
 * Rule followed throughout: never render a number we do not have. A missing
 * value says why it is missing — "no data", "calibrating", or "not stored by
 * NOOP" are all different states and the UI distinguishes them.
 */

const $ = (id) => document.getElementById(id);

const STAGE_ORDER = ['deep', 'rem', 'light', 'wake'];
const STAGE_LABEL = { deep: 'Deep', rem: 'REM', light: 'Light', wake: 'Awake' };
const RING_CIRCUMFERENCE = 2 * Math.PI * 86;

let META = null;
let VIEW_DAY = null;      // null = today
let TODAY_KEY = null;

// ---------- helpers ----------

async function getJSON(url) {
  const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const detail = body && (body.detail || body.error) || `HTTP ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body;
}

function fmt(value, meta) {
  if (value === null || value === undefined) return null;
  let v = Number(value);
  if (Number.isNaN(v)) return String(value);
  if (meta && meta.scale) v *= meta.scale;
  const p = meta && Number.isFinite(meta.precision) ? meta.precision : 0;
  const s = v.toFixed(p);
  return meta && meta.signed && v > 0 ? `+${s}` : s;
}

function minutesToHM(min) {
  if (min === null || min === undefined) return null;
  const total = Math.round(Number(min));
  return `${Math.floor(total / 60)}h ${String(total % 60).padStart(2, '0')}m`;
}

function shortDate(key) {
  const [y, m, d] = key.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC',
  });
}

function addDays(key, delta) {
  const [y, m, d] = key.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + delta));
  return dt.toISOString().slice(0, 10);
}

function banner(kind, title, body) {
  const el = $('banner');
  if (!kind) { el.className = 'banner hidden'; el.innerHTML = ''; return; }
  el.className = `banner ${kind}`;
  el.innerHTML = `<strong></strong><span></span>`;
  el.querySelector('strong').textContent = title;
  el.querySelector('span').textContent = body || '';
}

// ---------- rendering ----------

function renderRing(data) {
  const arc = $('ring-arc');
  const num = $('ring-num');
  const unit = $('ring-unit');
  const cap = $('ring-cap');
  const recovery = data.has_data ? data.metrics.recovery : null;

  num.classList.remove('small');

  if (recovery === null || recovery === undefined) {
    arc.style.strokeDashoffset = RING_CIRCUMFERENCE;
    arc.style.stroke = 'var(--muted)';
    unit.textContent = '';
    if (data.calibrating) {
      num.textContent = 'Calibrating';
      num.classList.add('small');
      cap.textContent = 'Recovery needs ~4 nights';
    } else {
      num.textContent = '--';
      cap.textContent = data.has_data ? 'Recovery unavailable' : 'No data';
    }
    return;
  }

  const pct = Math.max(0, Math.min(100, Number(recovery)));
  arc.style.strokeDashoffset = RING_CIRCUMFERENCE * (1 - pct / 100);
  arc.style.stroke = pct < 34 ? 'var(--red)' : pct < 67 ? 'var(--yellow)' : 'var(--green)';
  num.textContent = pct.toFixed(0);
  unit.textContent = '%';
  cap.textContent = 'Recovery — approximate';
}

function tileValue(key, data) {
  // Returns {text, unit, foot} or {empty: 'reason'}
  const meta = META.metrics[key] || {};

  if (key === 'heart_rate') {
    if (!data.heart_rate.available) return { empty: 'no HR table' };
    // "Last heart rate" is a live reading. Showing it while viewing a past day
    // would attribute today's number to that day.
    if (data.requested_day !== TODAY_KEY) return { empty: '—', foot: 'today only' };
    if (!data.heart_rate.last) return { empty: 'none today' };
    const mins = Math.round(data.heart_rate.age_seconds / 60);
    return {
      text: String(data.heart_rate.last.bpm), unit: 'bpm',
      foot: mins < 2 ? 'just now' : `${mins} min ago`,
    };
  }
  if (key === 'battery') {
    if (!data.battery_available) return { empty: 'not stored by NOOP' };
    return { text: fmt(data.battery.percent, meta), unit: '%' };
  }
  if (!data.has_data) return { empty: 'no data' };

  const raw = data.metrics[key];
  if (raw === null || raw === undefined) {
    if (key === 'recovery' && data.calibrating) return { empty: 'calibrating' };
    if (data.unavailable_fields.includes(key)) return { empty: 'not in this NOOP DB' };
    return { empty: 'no value' };
  }
  if (key === 'total_sleep_min') {
    return { text: minutesToHM(raw), unit: '', foot: fmtEfficiency(data) };
  }
  return { text: fmt(raw, meta), unit: meta.unit || '' };
}

function fmtEfficiency(data) {
  const eff = data.metrics.efficiency;
  return eff === null || eff === undefined ? '' : `${(eff * 100).toFixed(0)}% efficiency`;
}

function renderTiles(data) {
  const host = $('tiles');
  host.innerHTML = '';
  for (const key of META.today_tiles) {
    if (key === 'recovery') continue;            // shown as the ring
    const meta = META.metrics[key] || { label: key };
    const v = tileValue(key, data);

    const tile = document.createElement('button');
    tile.className = 'tile';
    tile.type = 'button';
    tile.addEventListener('click', () => openSheet(key));

    const label = document.createElement('span');
    label.className = 'tile-label';
    label.textContent = meta.label || key;

    const value = document.createElement('span');
    if (v.empty) {
      value.className = 'tile-value empty';
      value.textContent = v.empty;
    } else {
      value.className = 'tile-value';
      value.textContent = v.text;
      if (v.unit) {
        const u = document.createElement('span');
        u.className = 'tile-unit';
        u.textContent = v.unit;
        value.appendChild(u);
      }
    }

    const foot = document.createElement('span');
    foot.className = 'tile-foot';
    // An unavailable tile does not get a provenance label — there is no value to
    // qualify, and "approximate" under "no data" reads as noise.
    foot.textContent = v.foot
      || (v.empty ? '' : meta.kind === 'approximate' ? 'approximate' : meta.kind || '');

    tile.append(label, value, foot);
    host.appendChild(tile);
  }
}

function renderSleep(data) {
  const bar = $('stage-bar');
  const legend = $('stage-legend');
  bar.innerHTML = '';
  legend.innerHTML = '';

  const stages = (data.sleep && data.sleep.stage_minutes) || {};
  const total = Object.values(stages).reduce((a, b) => a + b, 0);

  if (!data.sleep.available) {
    legend.innerHTML = '<li>No sleep-session table resolved in this NOOP database.</li>';
    return;
  }
  if (total <= 0) {
    legend.innerHTML = '<li>No sleep session recorded for this day.</li>';
    return;
  }

  for (const stage of STAGE_ORDER) {
    const mins = stages[stage];
    if (!mins) continue;
    const seg = document.createElement('div');
    seg.className = `stage-seg ${stage}`;
    seg.style.width = `${(mins / total) * 100}%`;
    seg.title = `${STAGE_LABEL[stage]} ${Math.round(mins)} min`;
    bar.appendChild(seg);

    const li = document.createElement('li');
    const sw = document.createElement('span');
    sw.className = `swatch stage-seg ${stage}`;
    const txt = document.createElement('span');
    txt.textContent = `${STAGE_LABEL[stage]} `;
    const b = document.createElement('b');
    b.textContent = minutesToHM(mins);
    txt.appendChild(b);
    li.append(sw, txt);
    legend.appendChild(li);
  }
}

function renderSpark(samples) {
  const svg = $('hr-spark');
  const note = $('hr-note');
  svg.innerHTML = '';

  if (!samples || samples.length < 2) {
    note.textContent = 'No heart-rate samples for this day.';
    return;
  }
  const W = 600, H = 140, PAD = 8;
  const xs = samples.map((s) => s.ts);
  const ys = samples.map((s) => s.bpm);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const spanX = Math.max(1, x1 - x0), spanY = Math.max(1, y1 - y0);

  const px = (t) => ((t - x0) / spanX) * W;
  const py = (v) => H - PAD - ((v - y0) / spanY) * (H - 2 * PAD);

  const pts = samples.map((s) => `${px(s.ts).toFixed(1)},${py(s.bpm).toFixed(1)}`);
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  line.setAttribute('d', `M${pts.join('L')}`);

  const fill = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  fill.setAttribute('class', 'spark-fill');
  fill.setAttribute('d', `M${pts.join('L')}L${px(x1).toFixed(1)},${H}L${px(x0).toFixed(1)},${H}Z`);

  svg.append(fill, line);
  note.textContent = `${samples.length} samples · low ${y0} bpm · high ${y1} bpm`;
}

function renderStrap(state) {
  const dot = $('strap-dot');
  const text = $('strap-text');
  const note = $('strap-note');
  dot.className = `dot ${state.implemented ? 'ok' : 'idle'}`;
  text.textContent = state.implemented ? state.state : 'Not connected (by design)';
  note.textContent = `${state.message} ${state.constraint}`;
}

function renderDiagnostics(diag, data) {
  const host = $('diag-body');
  const s = diag.schema;
  const rows = [
    ['Database', `<code>${diag.db_path || 'not configured'}</code>`],
    ['Daily table', s.daily ? `<code>${s.daily.table}</code> (${Object.keys(s.daily.columns).length} fields)` : '<em>unresolved</em>'],
    ['Sleep table', s.sleep ? `<code>${s.sleep.table}</code>` : '<em>unresolved</em>'],
    ['HR table', s.heart_rate ? `<code>${s.heart_rate.table}</code>` : '<em>unresolved</em>'],
    ['Battery', s.battery ? `<code>${s.battery.table}.${s.battery.column}</code>` : '<em>not stored</em>'],
    ['Days stored', data.latest_day ? `latest ${data.latest_day}` : 'none'],
  ];
  let html = '<table>' + rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('') + '</table>';

  const notes = (s.notes || []).concat(data.suspect_values || []);
  if (notes.length) {
    html += '<ul>' + notes.map((n) => `<li>${escapeHTML(n)}</li>`).join('') + '</ul>';
  }
  html += `<p>${escapeHTML(diag.schema_note)}</p>`;
  host.innerHTML = html;
}

function escapeHTML(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ---------- detail sheet ----------

function openSheet(key) {
  const meta = META.metrics[key];
  if (!meta) return;
  $('sheet-title').textContent = meta.label || key;
  $('sheet-kind').textContent =
    meta.kind === 'approximate' ? 'Approximation — not clinical data'
      : meta.kind === 'derived' ? 'Derived on-device — not clinical data'
        : 'Sensor measurement — not clinical data';
  $('sheet-method').textContent = meta.method || '';
  $('sheet-note').textContent = meta.note || '';
  $('sheet').classList.remove('hidden');
}

function closeSheet() { $('sheet').classList.add('hidden'); }

// ---------- load ----------

async function load(day) {
  try {
    if (!META) META = await getJSON('/api/metrics/meta');
    $('disclaimer').textContent = META.disclaimer;

    // On first load, fall back to the most recent day so the screen is useful
    // even if the strap has not synced today. Once you have explicitly picked a
    // day, show that day or nothing — silently redirecting would be a lie.
    const qs = day ? `?day=${day}&fallback=false` : '';
    const data = await getJSON(`/api/today${qs}`);
    TODAY_KEY = TODAY_KEY || data.requested_day;
    VIEW_DAY = data.requested_day;

    $('day-label').textContent = shortDate(data.requested_day)
      + (data.requested_day === TODAY_KEY ? ' · today' : '');
    $('day-next').disabled = data.requested_day >= TODAY_KEY;

    // Data-state banner — the honest empty states.
    if (!data.has_data) {
      banner('warn', 'No data for this day',
        data.latest_day ? `Most recent day in NOOP's database is ${data.latest_day}.`
          : 'NOOP has no daily metrics stored yet.');
    } else if (data.fell_back) {
      banner('warn', `Showing ${shortDate(data.resolved_day)}`,
        `No row for ${shortDate(data.requested_day)}`
        + (data.days_behind > 0
          ? ` — ${data.days_behind} day(s) behind. Open NOOP and let the strap sync.`
          : '.'));
    } else if (data.suspect_values.length) {
      banner('warn', 'Value outside NOOP\'s own plausible range',
        data.suspect_values.join('; '));
    } else {
      banner(null);
    }

    renderRing(data);
    renderTiles(data);
    renderSleep(data);
    renderStrap(await getJSON('/api/strap/state'));

    const hrDay = data.resolved_day || data.requested_day;
    const hr = await getJSON(`/api/heart-rate?day=${hrDay}`);
    renderSpark(hr.samples);

    getJSON('/api/diagnostics')
      .then((d) => renderDiagnostics(d, data))
      .catch((e) => { $('diag-body').textContent = `diagnostics failed: ${e.message}`; });

  } catch (err) {
    banner('error', 'Cannot read NOOP\'s database', err.message);
    $('ring-num').textContent = '--';
    $('ring-cap').textContent = 'unavailable';
  }
}

$('day-prev').addEventListener('click', () => load(addDays(VIEW_DAY, -1)));
$('day-next').addEventListener('click', () => {
  if (VIEW_DAY < TODAY_KEY) load(addDays(VIEW_DAY, 1));
});
$('sheet-close').addEventListener('click', closeSheet);
$('sheet').addEventListener('click', (e) => { if (e.target.id === 'sheet') closeSheet(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSheet(); });

load(null);
