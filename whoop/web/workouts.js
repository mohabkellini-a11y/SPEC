/* Workout log: suggestions to confirm or dismiss, manual entry, history.
 *
 * Suggestions are never written to the log on your behalf. The detector has no
 * motion channel (see app/workout_detect.py), so it over-suggests on purpose and
 * under-suggests easy sessions — a confirm step is the honest interface for that.
 */

import { renderBars, renderScatter } from '/static/chart.js';

const $ = (id) => document.getElementById(id);

let WINDOW_DAYS = 90;

function fmtClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function fmtDuration(seconds) {
  const min = Math.round(seconds / 60);
  return min >= 60 ? `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, '0')}m` : `${min} min`;
}

function note(host, text) {
  host.innerHTML = '';
  const p = document.createElement('p');
  p.className = 'card-note';
  p.textContent = text;
  host.appendChild(p);
}

// --- suggestions -----------------------------------------------------------

function suggestionCard(s, reload) {
  const card = document.createElement('div');
  card.className = 'suggestion';

  const head = document.createElement('div');
  head.className = 'suggestion-head';
  const when = document.createElement('strong');
  when.textContent = `${fmtClock(s.start_ts)}–${fmtClock(s.end_ts)}`;
  const dur = document.createElement('span');
  dur.className = 'suggestion-dur';
  dur.textContent = fmtDuration(s.duration_s);
  head.append(when, dur);

  const detail = document.createElement('p');
  detail.className = 'suggestion-detail';
  detail.textContent = `avg ${s.avg_hr} bpm · peak ${s.peak_hr} bpm · ${s.day}`;

  const why = document.createElement('p');
  why.className = 'suggestion-why';
  why.textContent = `above ${s.threshold_bpm} bpm (your median ${s.typical_bpm} + margin), `
    + `averaging over the ${s.intensity_floor_bpm} bpm intensity floor`;

  const form = document.createElement('form');
  form.className = 'suggestion-form';

  const type = document.createElement('input');
  type.type = 'text';
  type.placeholder = 'What was it? (run, lift, ride…)';
  type.required = true;
  type.className = 'inp';

  const rpe = document.createElement('select');
  rpe.className = 'inp rpe';
  rpe.innerHTML = '<option value="">RPE</option>'
    + Array.from({ length: 10 }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join('');

  const actions = document.createElement('div');
  actions.className = 'suggestion-actions';
  const confirm = document.createElement('button');
  confirm.type = 'submit';
  confirm.className = 'btn primary';
  confirm.textContent = 'Confirm';
  const dismiss = document.createElement('button');
  dismiss.type = 'button';
  dismiss.className = 'btn';
  dismiss.textContent = 'Not a workout';
  actions.append(confirm, dismiss);

  form.append(type, rpe, actions);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    confirm.disabled = true;
    const res = await fetch(
      `/api/workouts/suggestions/${encodeURIComponent(s.key)}/confirm?day=${s.day}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: type.value.trim(), exertion: rpe.value ? Number(rpe.value) : null }),
      },
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      why.textContent = `Could not confirm: ${body.detail || res.status}`;
      why.classList.add('err');
      confirm.disabled = false;
      return;
    }
    reload();
  });

  dismiss.addEventListener('click', async () => {
    dismiss.disabled = true;
    await fetch(`/api/workouts/suggestions/${encodeURIComponent(s.key)}/dismiss?day=${s.day}`,
      { method: 'POST' });
    reload();
  });

  card.append(head, detail, why, form);
  return card;
}

async function loadSuggestions(reload) {
  const host = $('suggestion-list');
  host.innerHTML = '<p class="card-note">looking…</p>';
  try {
    const data = await fetch('/api/workouts/suggestions?days=7').then((r) => r.json());
    if (!data.available) {
      note(host, data.reason || 'Suggestions unavailable.');
      return;
    }
    host.innerHTML = '';
    if (!data.suggestions.length) {
      note(host, data.n_hidden
        ? `Nothing new. ${data.n_hidden} block(s) already logged or dismissed.`
        : 'No elevated-HR blocks found in the last 7 days.');
    } else {
      for (const s of data.suggestions) host.appendChild(suggestionCard(s, reload));
    }
    $('suggestion-method').textContent = data.method_note;
  } catch (err) {
    note(host, `Could not load suggestions: ${err.message}`);
  }
}

// --- history ---------------------------------------------------------------

function workoutRow(w, reload) {
  const row = document.createElement('div');
  row.className = 'workout-row';

  const main = document.createElement('div');
  main.className = 'workout-main';
  const title = document.createElement('strong');
  title.textContent = w.type;
  const meta = document.createElement('span');
  meta.className = 'workout-meta';
  const bits = [w.day, fmtDuration(w.duration_s)];
  if (w.exertion) bits.push(`RPE ${w.exertion}`);
  if (w.avg_hr) bits.push(`avg ${Math.round(w.avg_hr)} bpm`);
  meta.textContent = bits.join(' · ');
  main.append(title, meta);
  if (w.notes) {
    const n = document.createElement('span');
    n.className = 'workout-notes';
    n.textContent = w.notes;
    main.appendChild(n);
  }

  const badge = document.createElement('span');
  badge.className = `pill ${w.source === 'confirmed' ? 'derived' : ''}`;
  badge.textContent = w.source === 'confirmed' ? 'from strap' : 'manual';

  const del = document.createElement('button');
  del.className = 'icon-btn';
  del.type = 'button';
  del.setAttribute('aria-label', `Delete ${w.type} on ${w.day}`);
  del.textContent = '×';
  del.addEventListener('click', async () => {
    if (!window.confirm(`Delete this ${w.type} on ${w.day}?`)) return;
    await fetch(`/api/workouts/${w.id}`, { method: 'DELETE' });
    reload();
  });

  row.append(main, badge, del);
  return row;
}

async function loadHistory(reload) {
  const host = $('workout-list');
  try {
    const data = await fetch(`/api/workouts?days=${WINDOW_DAYS}`).then((r) => r.json());
    host.innerHTML = '';
    if (!data.workouts.length) {
      note(host, 'Nothing logged yet.');
      return;
    }
    for (const w of data.workouts) host.appendChild(workoutRow(w, reload));
  } catch (err) {
    note(host, `Could not load history: ${err.message}`);
  }
}

async function loadSummary() {
  try {
    const data = await fetch(`/api/workouts/summary?days=${WINDOW_DAYS}`).then((r) => r.json());
    $('volume-total').textContent = data.n_workouts
      ? `${data.n_workouts} workouts · ${Math.round(data.total_minutes)} min total`
      : 'no workouts in this window';
    renderBars($('volume-chart'), data.weekly_volume);

    const sr = data.strain_recovery;
    if (!sr.available) {
      renderScatter($('scatter-chart'), [], { emptyText: sr.reason || 'unavailable' });
    } else {
      renderScatter($('scatter-chart'), sr.points);
    }
    $('scatter-note').textContent = sr.note;
  } catch (err) {
    $('volume-total').textContent = `Could not load summary: ${err.message}`;
  }
}

// --- manual entry ----------------------------------------------------------

function initManualForm(reload) {
  const form = $('workout-form');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      day: $('w-day').value,
      type: $('w-type').value.trim(),
      duration_s: Math.round(Number($('w-duration').value) * 60),
      exertion: $('w-rpe').value ? Number($('w-rpe').value) : null,
      notes: $('w-notes').value.trim() || null,
    };
    const res = await fetch('/api/workouts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await res.json().catch(() => ({}));
    const msg = $('workout-form-msg');
    if (!res.ok) {
      msg.textContent = typeof payload.detail === 'string'
        ? payload.detail : 'Could not save that workout.';
      msg.className = 'form-msg err';
      return;
    }
    msg.textContent = 'Logged.';
    msg.className = 'form-msg ok';
    $('w-type').value = '';
    $('w-duration').value = '';
    $('w-notes').value = '';
    reload();
  });
}

export async function loadWorkouts() {
  const reload = () => loadWorkouts();
  await Promise.all([loadSuggestions(reload), loadHistory(reload), loadSummary()]);
}

export function initWorkouts(today) {
  $('w-day').value = today;
  $('w-day').max = today;
  initManualForm(() => loadWorkouts());
  document.querySelectorAll('.wrange-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      WINDOW_DAYS = Number(chip.dataset.days);
      document.querySelectorAll('.wrange-chip').forEach((c) => c.classList.toggle('is-on', c === chip));
      loadWorkouts();
    });
  });
}
