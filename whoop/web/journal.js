/* Habit journal — built for speed.
 *
 * The brief asks for a day's entry in under fifteen seconds on a phone, so:
 *   - no Save button; every tap writes immediately
 *   - one tap records the common case (a yes)
 *   - controls are full-width rows, thumb-sized, no precision needed
 *
 * Three states per habit, not two: unset, yes, and an explicit no. "I did not
 * drink" and "I did not log" are different facts, and Phase 5's correlations
 * will need to tell them apart rather than reading silence as zero.
 */

const $ = (id) => document.getElementById(id);

let HABITS = [];
let DAY = null;
let noteTimer = null;
let numberTimers = new Map();

function status(text, kind = '') {
  const el = $('journal-status');
  el.textContent = text;
  el.className = `journal-status ${kind}`;
  if (text && kind === 'ok') {
    clearTimeout(status._t);
    status._t = setTimeout(() => { el.textContent = ''; el.className = 'journal-status'; }, 1400);
  }
}

async function put(body) {
  status('saving…');
  const res = await fetch(`/api/journal/${DAY}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    status((payload && payload.detail) || `save failed (${res.status})`, 'err');
    throw new Error((payload && payload.detail) || res.status);
  }
  status('saved', 'ok');
  // The response carries the day's new state, so the counter tracks every tap
  // rather than only refreshing on a reload.
  if (payload && typeof payload.entry_count === 'number') setCount(payload.entry_count);
  return payload;
}

function setCount(n) {
  $('journal-count').textContent = n
    ? `${n} logged` : 'nothing logged yet';
}

/** unset -> yes -> no -> unset. One tap for the common case. */
function nextBoolValue(current) {
  if (current === null || current === undefined) return 1;
  if (current === 1) return 0;
  return null;
}

function boolControl(habit) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'habit-row bool';
  row.dataset.state = habit.value === 1 ? 'yes' : habit.value === 0 ? 'no' : 'unset';

  const label = document.createElement('span');
  label.className = 'habit-label';
  label.textContent = habit.label;

  const state = document.createElement('span');
  state.className = 'habit-state';
  state.textContent = habit.value === 1 ? 'Yes' : habit.value === 0 ? 'No' : '—';

  row.append(label, state);
  row.addEventListener('click', async () => {
    const next = nextBoolValue(habit.value);
    habit.value = next;
    row.dataset.state = next === 1 ? 'yes' : next === 0 ? 'no' : 'unset';
    state.textContent = next === 1 ? 'Yes' : next === 0 ? 'No' : '—';
    await put({ values: { [habit.id]: next } }).catch(() => { render(); });
  });
  return row;
}

function scaleControl(habit) {
  const wrap = document.createElement('div');
  wrap.className = 'habit-row scale';

  const label = document.createElement('span');
  label.className = 'habit-label';
  label.textContent = habit.label;
  wrap.appendChild(label);

  const lo = habit.min_value ?? 1;
  const hi = habit.max_value ?? 5;
  const group = document.createElement('div');
  group.className = 'scale-group';

  for (let v = lo; v <= hi; v += 1) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'scale-btn';
    btn.textContent = String(v);
    if (habit.value === v) btn.classList.add('is-on');
    btn.addEventListener('click', async () => {
      // Tapping the selected value clears it — undo without a separate control.
      const next = habit.value === v ? null : v;
      habit.value = next;
      group.querySelectorAll('.scale-btn').forEach((b) => {
        b.classList.toggle('is-on', next !== null && Number(b.textContent) === next);
      });
      await put({ values: { [habit.id]: next } }).catch(() => { render(); });
    });
    group.appendChild(btn);
  }
  wrap.appendChild(group);
  return wrap;
}

function numberControl(habit) {
  const wrap = document.createElement('div');
  wrap.className = 'habit-row number';

  const label = document.createElement('span');
  label.className = 'habit-label';
  label.textContent = habit.label + (habit.unit ? ` (${habit.unit})` : '');

  const input = document.createElement('input');
  input.type = 'number';
  input.inputMode = 'decimal';
  input.className = 'habit-number';
  input.value = habit.value ?? '';
  input.placeholder = '—';
  if (habit.min_value !== null && habit.min_value !== undefined) input.min = habit.min_value;
  if (habit.max_value !== null && habit.max_value !== undefined) input.max = habit.max_value;

  input.addEventListener('input', () => {
    clearTimeout(numberTimers.get(habit.id));
    numberTimers.set(habit.id, setTimeout(async () => {
      const raw = input.value.trim();
      const next = raw === '' ? null : Number(raw);
      if (next !== null && Number.isNaN(next)) return;
      habit.value = next;
      await put({ values: { [habit.id]: next } }).catch(() => {});
    }, 500));
  });

  wrap.append(label, input);
  return wrap;
}

function render() {
  const host = $('journal-habits');
  host.innerHTML = '';
  if (!HABITS.length) {
    host.innerHTML = '<p class="card-note">No habits defined yet. Add one below.</p>';
    return;
  }
  for (const habit of HABITS) {
    if (habit.type === 'bool') host.appendChild(boolControl(habit));
    else if (habit.type === 'scale') host.appendChild(scaleControl(habit));
    else host.appendChild(numberControl(habit));
  }
}

export async function loadJournal(day) {
  DAY = day;
  const res = await fetch(`/api/journal/${day}`);
  if (!res.ok) {
    $('journal-habits').innerHTML = '<p class="card-note">Could not load the journal.</p>';
    return;
  }
  const data = await res.json();
  HABITS = data.habits;
  render();
  $('journal-note').value = data.note || '';
  setCount(data.entry_count);
}

export function initJournal(onDayNeeded) {
  $('journal-note').addEventListener('input', (e) => {
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => put({ note: e.target.value }).catch(() => {}), 600);
  });

  $('habit-add-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const label = $('habit-label').value.trim();
    const type = $('habit-type').value;
    const unit = $('habit-unit').value.trim();
    if (!label) return;
    // Key is derived from the label so there is one field fewer to fill in.
    const key = label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '')
      || `habit_${Date.now()}`;

    const res = await fetch('/api/habits', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, label, type, unit: unit || null }),
    });
    const payload = await res.json().catch(() => null);
    if (!res.ok) {
      status((payload && payload.detail) || 'could not add habit', 'err');
      return;
    }
    $('habit-label').value = '';
    $('habit-unit').value = '';
    status('habit added', 'ok');
    loadJournal(onDayNeeded());
  });

  $('habit-type').addEventListener('change', (e) => {
    // Only a free number needs a unit.
    $('habit-unit').classList.toggle('hidden', e.target.value !== 'number');
  });
}
