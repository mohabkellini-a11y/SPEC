/* Minimal SVG line chart. No dependencies, no build step.
 *
 * The one rule it exists to enforce: a gap in the data is a gap in the line.
 * Nulls break the path rather than being bridged, because a bridged line over a
 * week the strap was on the charger is a drawing of data that does not exist.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

const VIEW_W = 320;
const VIEW_H = 120;
const PAD = { top: 10, right: 6, bottom: 16, left: 30 };

function el(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/** Split an array into runs of consecutive non-null values. */
function segments(values) {
  const runs = [];
  let current = null;
  values.forEach((v, i) => {
    if (v === null || v === undefined) { current = null; return; }
    if (!current) { current = []; runs.push(current); }
    current.push([i, v]);
  });
  return runs;
}

function niceBounds(values) {
  const nums = values.filter((v) => v !== null && v !== undefined);
  if (!nums.length) return null;
  let lo = Math.min(...nums);
  let hi = Math.max(...nums);
  if (lo === hi) { lo -= 1; hi += 1; }          // flat series still needs a band
  const padding = (hi - lo) * 0.12;
  return { lo: lo - padding, hi: hi + padding, dataLo: Math.min(...nums), dataHi: Math.max(...nums) };
}

/**
 * Draw a line chart into an <svg>.
 *
 * @param {SVGElement} svg      target, emptied first
 * @param {object} opts
 *   days       {string[]}          dense day keys (x axis)
 *   values     {(number|null)[]}   raw daily values, aligned to days
 *   rolling    {(number|null)[]}   rolling mean, aligned to days (optional)
 *   color      {string}            CSS colour for the rolling line
 *   precision  {number}            decimals on axis labels
 *   scale      {number}            multiply values for display (e.g. 0..1 -> %)
 */
export function renderLineChart(svg, opts) {
  const { days, color = 'var(--accent)', precision = 0, scale = 1 } = opts;
  const values = (opts.values || []).map((v) => (v === null || v === undefined ? null : v * scale));
  const rolling = (opts.rolling || []).map((v) => (v === null || v === undefined ? null : v * scale));

  svg.setAttribute('viewBox', `0 0 ${VIEW_W} ${VIEW_H}`);
  svg.innerHTML = '';

  const bounds = niceBounds(values);
  if (!bounds) {
    svg.appendChild(el('text', {
      x: VIEW_W / 2, y: VIEW_H / 2, 'text-anchor': 'middle',
      class: 'chart-empty',
    })).textContent = 'no data in this window';
    return;
  }

  const plotW = VIEW_W - PAD.left - PAD.right;
  const plotH = VIEW_H - PAD.top - PAD.bottom;
  const n = Math.max(1, days.length - 1);
  const px = (i) => PAD.left + (i / n) * plotW;
  const py = (v) => PAD.top + (1 - (v - bounds.lo) / (bounds.hi - bounds.lo)) * plotH;

  // --- gridlines + y labels (data min/max, not the padded bounds) ---------
  for (const value of [bounds.dataHi, bounds.dataLo]) {
    const y = py(value);
    svg.appendChild(el('line', { x1: PAD.left, x2: VIEW_W - PAD.right, y1: y, y2: y, class: 'chart-grid' }));
    const label = el('text', { x: PAD.left - 4, y: y + 3, 'text-anchor': 'end', class: 'chart-axis' });
    label.textContent = value.toFixed(precision);
    svg.appendChild(label);
  }

  // --- raw daily line, one path per unbroken run -------------------------
  for (const run of segments(values)) {
    if (run.length === 1) {
      const [i, v] = run[0];
      svg.appendChild(el('circle', { cx: px(i), cy: py(v), r: 1.6, class: 'chart-point' }));
      continue;
    }
    const d = run.map(([i, v], k) => `${k ? 'L' : 'M'}${px(i).toFixed(2)},${py(v).toFixed(2)}`).join('');
    svg.appendChild(el('path', { d, class: 'chart-raw' }));
  }

  // --- rolling mean on top ------------------------------------------------
  for (const run of segments(rolling)) {
    if (run.length < 2) continue;
    const d = run.map(([i, v], k) => `${k ? 'L' : 'M'}${px(i).toFixed(2)},${py(v).toFixed(2)}`).join('');
    svg.appendChild(el('path', { d, class: 'chart-rolling', style: `stroke:${color}` }));
  }

  // --- x labels: first and last day --------------------------------------
  if (days.length) {
    const first = el('text', { x: PAD.left, y: VIEW_H - 4, class: 'chart-axis' });
    first.textContent = shortDay(days[0]);
    const last = el('text', { x: VIEW_W - PAD.right, y: VIEW_H - 4, 'text-anchor': 'end', class: 'chart-axis' });
    last.textContent = shortDay(days[days.length - 1]);
    svg.append(first, last);
  }
}

function shortDay(key) {
  const [y, m, d] = key.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d))
    .toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

/** Coverage bar: how much of the window actually had data. */
export function renderCoverage(node, summary) {
  const pct = Math.round((summary.coverage || 0) * 100);
  node.innerHTML = '';
  const bar = document.createElement('div');
  bar.className = 'coverage-bar';
  const fill = document.createElement('div');
  fill.className = 'coverage-fill';
  fill.style.width = `${pct}%`;
  if (pct < 50) fill.classList.add('low');
  bar.appendChild(fill);
  const text = document.createElement('span');
  text.className = 'coverage-text';
  text.textContent = `${summary.n_values} of ${summary.n_days} days`;
  node.append(bar, text);
}

/** Weekly volume bars. `bars` = [{week, minutes, count}]. */
export function renderBars(svg, bars, opts = {}) {
  const color = opts.color || 'var(--accent)';
  svg.setAttribute('viewBox', `0 0 ${VIEW_W} ${VIEW_H}`);
  svg.innerHTML = '';

  if (!bars.length) {
    svg.appendChild(el('text', {
      x: VIEW_W / 2, y: VIEW_H / 2, 'text-anchor': 'middle', class: 'chart-empty',
    })).textContent = 'no workouts logged in this window';
    return;
  }

  const plotW = VIEW_W - PAD.left - PAD.right;
  const plotH = VIEW_H - PAD.top - PAD.bottom;
  const max = Math.max(...bars.map((b) => b.minutes), 1);
  const slot = plotW / bars.length;
  const width = Math.max(2, Math.min(slot * 0.68, 26));

  const top = el('line', {
    x1: PAD.left, x2: VIEW_W - PAD.right, y1: PAD.top, y2: PAD.top, class: 'chart-grid',
  });
  svg.appendChild(top);
  const maxLabel = el('text', { x: PAD.left - 4, y: PAD.top + 3, 'text-anchor': 'end', class: 'chart-axis' });
  maxLabel.textContent = `${Math.round(max)}m`;
  svg.appendChild(maxLabel);

  bars.forEach((bar, i) => {
    const h = (bar.minutes / max) * plotH;
    const x = PAD.left + i * slot + (slot - width) / 2;
    const rect = el('rect', {
      x: x.toFixed(2), y: (PAD.top + plotH - h).toFixed(2),
      width: width.toFixed(2), height: Math.max(0.5, h).toFixed(2),
      rx: 2, style: `fill:${color}`, class: 'chart-bar',
    });
    rect.appendChild(el('title')).textContent =
      `${bar.week}: ${bar.minutes} min over ${bar.count} workout(s)`;
    svg.appendChild(rect);
  });

  const baseline = el('line', {
    x1: PAD.left, x2: VIEW_W - PAD.right,
    y1: PAD.top + plotH, y2: PAD.top + plotH, class: 'chart-grid',
  });
  svg.appendChild(baseline);

  const first = el('text', { x: PAD.left, y: VIEW_H - 4, class: 'chart-axis' });
  first.textContent = bars[0].week.replace(/^\d{4}-/, '');
  const last = el('text', { x: VIEW_W - PAD.right, y: VIEW_H - 4, 'text-anchor': 'end', class: 'chart-axis' });
  last.textContent = bars[bars.length - 1].week.replace(/^\d{4}-/, '');
  svg.append(first, last);
}

/**
 * Strain (x) vs recovery (y) scatter.
 * Axes are fixed to each metric's real domain — 0-21 and 0-100 — rather than to
 * the data, so the cloud cannot look tighter or looser than it is.
 */
export function renderScatter(svg, points, opts = {}) {
  svg.setAttribute('viewBox', `0 0 ${VIEW_W} ${VIEW_H + 10}`);
  svg.innerHTML = '';

  if (!points.length) {
    svg.appendChild(el('text', {
      x: VIEW_W / 2, y: VIEW_H / 2, 'text-anchor': 'middle', class: 'chart-empty',
    })).textContent = opts.emptyText || 'no days with both strain and recovery';
    return;
  }

  const xMax = opts.xMax || 21;
  const yMax = opts.yMax || 100;
  const plotW = VIEW_W - PAD.left - PAD.right;
  const plotH = VIEW_H - PAD.top - PAD.bottom;
  const px = (v) => PAD.left + (v / xMax) * plotW;
  const py = (v) => PAD.top + (1 - v / yMax) * plotH;

  // Recovery band lines at NOOP's red/yellow/green thresholds.
  for (const [value, cls] of [[34, 'red'], [67, 'green']]) {
    const y = py(value);
    svg.appendChild(el('line', {
      x1: PAD.left, x2: VIEW_W - PAD.right, y1: y, y2: y,
      class: `chart-grid band-${cls}`,
    }));
  }

  for (const [value, anchor] of [[yMax, 'end'], [0, 'end']]) {
    const label = el('text', { x: PAD.left - 4, y: py(value) + 3, 'text-anchor': anchor, class: 'chart-axis' });
    label.textContent = String(value);
    svg.appendChild(label);
  }

  svg.appendChild(el('line', {
    x1: PAD.left, x2: VIEW_W - PAD.right, y1: py(0), y2: py(0), class: 'chart-grid',
  }));

  for (const point of points) {
    const dot = el('circle', {
      cx: px(Math.min(point.strain, xMax)).toFixed(2),
      cy: py(Math.min(point.recovery, yMax)).toFixed(2),
      r: 2.2, class: 'scatter-dot',
    });
    dot.appendChild(el('title')).textContent =
      `${point.day}: strain ${point.strain.toFixed(1)}, recovery ${Math.round(point.recovery)}%`;
    svg.appendChild(dot);
  }

  const xlab = el('text', { x: PAD.left + plotW / 2, y: VIEW_H + 6, 'text-anchor': 'middle', class: 'chart-axis' });
  xlab.textContent = `day strain 0–${xMax}  ·  recovery % on y`;
  svg.appendChild(xlab);
}
