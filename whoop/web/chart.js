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
