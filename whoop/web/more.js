/* Correlations, export, and PWA install status.
 *
 * The correlations view is the one screen most likely to be over-read, so it
 * leads with the caveats rather than burying them: no headline number appears
 * until both groups have enough days, and "descriptive, not causal" is on screen
 * every time, not in a footnote.
 */

import { renderLineChart } from '/static/chart.js';

const $ = (id) => document.getElementById(id);

let OPTIONS = null;
let CORR_DAYS = 30;

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && (body.detail || body.error);
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`);
  }
  return body;
}

// ---------- correlations ----------

function groupBar(group, maxMean) {
  const row = document.createElement('div');
  row.className = 'group-row';

  const label = document.createElement('div');
  label.className = 'group-label';
  label.textContent = group.label;

  const barWrap = document.createElement('div');
  barWrap.className = 'group-bar-wrap';
  const bar = document.createElement('div');
  bar.className = 'group-bar';
  bar.style.width = maxMean ? `${Math.max(2, (group.mean / maxMean) * 100)}%` : '2%';
  barWrap.appendChild(bar);

  const value = document.createElement('div');
  value.className = 'group-value';
  value.textContent = group.mean === null ? '—' : String(group.mean);
  const n = document.createElement('span');
  n.className = 'group-n';
  n.textContent = ` ${group.n}d`;
  value.appendChild(n);

  row.append(label, barWrap, value);
  return row;
}

function renderCorrelation(data) {
  const host = $('corr-result');
  host.innerHTML = '';

  const summary = document.createElement('p');
  summary.className = data.reliable ? 'corr-summary' : 'corr-summary weak';
  summary.textContent = data.summary;
  host.appendChild(summary);

  if (data.enough_data) {
    const means = data.groups.map((g) => g.mean).filter((m) => m !== null);
    const max = means.length ? Math.max(...means) : 0;
    const bars = document.createElement('div');
    bars.className = 'group-bars';
    for (const group of data.groups) bars.appendChild(groupBar(group, max));
    host.appendChild(bars);

    if (data.pearson_r !== null && data.pearson_r !== undefined) {
      const r = document.createElement('p');
      r.className = 'card-note';
      r.textContent = `Pearson r = ${data.pearson_r} across ${data.n_pairs} paired days. `
        + 'A correlation coefficient, not an effect.';
      host.appendChild(r);
    }

    // Overlay: the metric timeline with the habit's days marked underneath.
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'chart');
    host.appendChild(svg);
    renderLineChart(svg, {
      days: data.timeline.days,
      values: data.timeline.metric,
      rolling: [],
      precision: 0,
    });

    const strip = document.createElement('div');
    strip.className = 'habit-strip';
    for (const value of data.timeline.habit) {
      const cell = document.createElement('span');
      cell.className = 'habit-cell';
      if (value === null || value === undefined) cell.classList.add('unlogged');
      else if (value > 0) cell.classList.add('on');
      else cell.classList.add('off');
      strip.appendChild(cell);
    }
    host.appendChild(strip);

    const legend = document.createElement('p');
    legend.className = 'card-note';
    legend.textContent = `Line: ${data.metric_label} per day. Strip: ${data.habit_label} `
      + '— filled = logged and present, hollow = logged as no, blank = not logged.';
    host.appendChild(legend);
  }

  const caveats = document.createElement('ul');
  caveats.className = 'caveats';
  for (const text of data.caveats) {
    const li = document.createElement('li');
    li.textContent = text;
    caveats.appendChild(li);
  }
  host.appendChild(caveats);
}

async function loadCorrelation() {
  const host = $('corr-result');
  const habitId = $('corr-habit').value;
  const metric = $('corr-metric').value;
  if (!habitId || !metric) return;

  host.innerHTML = '<p class="card-note">calculating…</p>';
  try {
    renderCorrelation(await getJSON(
      `/api/correlations?habit_id=${habitId}&metric=${metric}&days=${CORR_DAYS}&lag=1`));
  } catch (err) {
    host.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'card-note warn-text';
    p.textContent = err.message;
    host.appendChild(p);
  }
}

// ---------- export ----------

async function loadExportCounts() {
  try {
    const data = await getJSON('/api/export/summary');
    const counts = data.counts;
    const parts = [];
    for (const [name, n] of Object.entries(counts)) {
      if (n > 0) parts.push(`${n} ${name.replace(/_/g, ' ')}`);
    }
    $('export-counts').textContent = parts.length
      ? parts.join(' · ') : 'nothing logged yet';
  } catch (err) {
    $('export-counts').textContent = `could not count: ${err.message}`;
  }
}

// ---------- install status ----------

function renderInstallState() {
  const el = $('pwa-state');
  const standalone = window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;

  if (standalone) {
    el.textContent = 'Running as an installed app. ' + (
      'serviceWorker' in navigator && navigator.serviceWorker.controller
        ? 'Offline caching is active.'
        : 'Offline caching is off — see below.');
    return;
  }

  const secure = window.isSecureContext;
  const lines = [
    'iPhone: open this page in Safari, tap Share, then "Add to Home Screen". '
    + 'It opens full-screen with no browser chrome.',
  ];
  if (!secure) {
    // The honest limitation: a plain http:// LAN address is not a secure
    // context, so the browser refuses to register a service worker.
    lines.push(
      'Offline caching is unavailable on this address: browsers only allow it on '
      + 'https:// or localhost, and this is plain http on your LAN. Installing '
      + 'still works — the app just needs the server reachable to load. See the '
      + 'README for how to serve over HTTPS if you want offline support.');
  }
  el.textContent = lines.join(' ');
}

// ---------- offline banner ----------

/** Called by app.js whenever a response comes back from the service-worker cache. */
export function showStale(cachedAtMs) {
  const el = $('offline-banner');
  const when = cachedAtMs
    ? new Date(cachedAtMs).toLocaleTimeString(undefined,
      { hour: '2-digit', minute: '2-digit' })
    : null;
  el.className = 'banner warn';
  el.innerHTML = '<strong></strong><span></span>';
  el.querySelector('strong').textContent = 'Offline — showing saved data';
  el.querySelector('span').textContent = when
    ? `Last updated ${when}. These numbers are not current.`
    : 'These numbers are not current.';
}

export function clearStale() {
  const el = $('offline-banner');
  el.className = 'banner hidden';
  el.innerHTML = '';
}

// ---------- init ----------

export async function loadMore() {
  await Promise.all([loadExportCounts(), loadCorrelation()]);
  renderInstallState();
}

export async function initMore() {
  try {
    OPTIONS = await getJSON('/api/correlations/options');
  } catch {
    $('corr-result').textContent = 'Could not load correlation options.';
    return;
  }

  const habitSelect = $('corr-habit');
  for (const habit of OPTIONS.habits) {
    const option = document.createElement('option');
    option.value = habit.id;
    option.textContent = habit.archived ? `${habit.label} (archived)` : habit.label;
    habitSelect.appendChild(option);
  }
  const metricSelect = $('corr-metric');
  for (const metric of OPTIONS.metrics) {
    const option = document.createElement('option');
    option.value = metric.key;
    option.textContent = `vs ${metric.label}`;
    if (metric.key === 'recovery') option.selected = true;
    metricSelect.appendChild(option);
  }

  habitSelect.addEventListener('change', loadCorrelation);
  metricSelect.addEventListener('change', loadCorrelation);
  document.querySelectorAll('.corr-range').forEach((chip) => {
    chip.addEventListener('click', () => {
      CORR_DAYS = Number(chip.dataset.days);
      document.querySelectorAll('.corr-range').forEach(
        (c) => c.classList.toggle('is-on', c === chip));
      loadCorrelation();
    });
  });
}

/** Register the service worker, but only where the browser will allow it. */
export function registerServiceWorker() {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register('/sw.js').catch(() => {
    /* Registration failing is not fatal — the app works online regardless. */
  });
}
