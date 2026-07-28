/* Alarms and timers.
 *
 * The screen's job is to never let you believe an alarm is set when it is not.
 * Three things follow from that:
 *   - the strap banner is always visible, and says idle/failed plainly
 *   - each alarm shows whether it is really ON THE STRAP or merely queued here
 *   - a failure is red, quotes the reason, and offers Retry — it is not a toast
 *     that scrolls away
 */

const $ = (id) => document.getElementById(id);

const QUICK_TIMERS = [
  { label: '10 min', seconds: 600 },
  { label: '20 min', seconds: 1200 },
  { label: '30 min', seconds: 1800 },
  { label: '90 min', seconds: 5400 },
];

let TICKER = null;

function fmtClock(unix) {
  return new Date(unix * 1000).toLocaleTimeString(undefined,
    { hour: '2-digit', minute: '2-digit' });
}

function fmtDay(unix) {
  const d = new Date(unix * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  if (sameDay) return 'today';
  const tomorrow = new Date(today.getTime() + 86400000);
  if (d.toDateString() === tomorrow.toDateString()) return 'tomorrow';
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

function countdown(seconds) {
  if (seconds <= 0) return 'now';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `in ${h}h ${String(m).padStart(2, '0')}m`;
  if (m) return `in ${m}m ${String(s).padStart(2, '0')}s`;
  return `in ${s}s`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail;
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`);
  }
  return body;
}

// ---------- strap banner ----------

function renderStrap(state) {
  const dot = $('alarm-strap-dot');
  const text = $('alarm-strap-text');
  const note = $('alarm-strap-note');
  const testBtn = $('strap-test-btn');

  const available = state.available;
  dot.className = `dot ${available ? 'ok' : 'err'}`;
  text.textContent = available
    ? `Ready — ${state.state}`
    : 'Strap not set up';

  const last = state.last_send;
  if (!available) {
    note.textContent = state.hint || 'No transport available.';
    note.className = 'card-note warn-text';
  } else if (last && !last.ok) {
    note.textContent = `Last send failed: ${last.message}`;
    note.className = 'card-note warn-text';
  } else {
    note.textContent = state.connection_policy;
    note.className = 'card-note';
  }
  testBtn.disabled = !available;
}

// ---------- alarm list ----------

function alarmRow(alarm, reload) {
  const row = document.createElement('div');
  row.className = `alarm-row state-${alarm.state}`;
  row.dataset.fireAt = alarm.fire_at;

  const main = document.createElement('div');
  main.className = 'alarm-main';

  const timeEl = document.createElement('div');
  timeEl.className = 'alarm-time';
  timeEl.textContent = fmtClock(alarm.fire_at);
  const when = document.createElement('span');
  when.className = 'alarm-when';
  when.textContent = ` ${fmtDay(alarm.fire_at)}`;
  timeEl.appendChild(when);

  const label = document.createElement('div');
  label.className = 'alarm-label';
  label.textContent = alarm.label || (alarm.kind === 'timer' ? 'Timer' : 'Alarm');

  const status = document.createElement('div');
  const onStrap = alarm.on_strap;
  status.className = `alarm-status ${onStrap ? 'good' : alarm.state === 'failed' ? 'bad' : 'queued'}`;
  status.textContent = alarm.status;

  const away = document.createElement('span');
  away.className = 'alarm-countdown';
  away.textContent = countdown(alarm.seconds_away);

  main.append(timeEl, label, status);
  if (alarm.note) {
    const note = document.createElement('div');
    note.className = 'alarm-note';
    note.textContent = alarm.note;
    main.appendChild(note);
  }
  if (alarm.state === 'failed' && alarm.last_error) {
    const err = document.createElement('div');
    err.className = 'alarm-error';
    err.textContent = alarm.last_error;
    main.appendChild(err);
  }

  const actions = document.createElement('div');
  actions.className = 'alarm-actions';

  if (alarm.state === 'failed') {
    const retry = document.createElement('button');
    retry.className = 'btn small primary';
    retry.textContent = 'Retry';
    retry.addEventListener('click', async () => {
      retry.disabled = true;
      retry.textContent = 'trying…';
      try {
        const res = await api(`/api/alarms/${alarm.id}/retry`, { method: 'POST' });
        if (!res.ok) banner('error', 'Still could not set it', res.message);
      } catch (err) {
        banner('error', 'Retry failed', err.message);
      }
      reload();
    });
    actions.appendChild(retry);
  }

  const del = document.createElement('button');
  del.className = 'icon-btn';
  del.type = 'button';
  del.textContent = '×';
  del.setAttribute('aria-label', `Cancel alarm at ${fmtClock(alarm.fire_at)}`);
  del.addEventListener('click', async () => {
    try {
      const res = await api(`/api/alarms/${alarm.id}`, { method: 'DELETE' });
      // Cancelling something already on the strap cannot be guaranteed, and the
      // user needs to know that before they rely on the silence.
      if (res.strap_may_still_fire) {
        banner('warn', 'Cancelled here — the strap may still buzz', res.warning);
      }
    } catch (err) {
      banner('error', 'Could not cancel', err.message);
    }
    reload();
  });
  actions.appendChild(del);

  row.append(main, away, actions);
  return row;
}

function banner(kind, title, body) {
  const el = $('alarm-banner');
  if (!kind) { el.className = 'banner hidden'; el.innerHTML = ''; return; }
  el.className = `banner ${kind}`;
  el.innerHTML = '<strong></strong><span></span>';
  el.querySelector('strong').textContent = title;
  el.querySelector('span').textContent = body || '';
}

function retickCountdowns() {
  const now = Math.floor(Date.now() / 1000);
  document.querySelectorAll('#alarm-list .alarm-row').forEach((row) => {
    const el = row.querySelector('.alarm-countdown');
    if (el) el.textContent = countdown(Number(row.dataset.fireAt) - now);
  });
}

// ---------- loading ----------

export async function loadAlarms() {
  const reload = () => loadAlarms();
  const host = $('alarm-list');

  try {
    const [listing, state] = await Promise.all([
      api('/api/alarms'),
      api('/api/strap/state'),
    ]);
    renderStrap(state);

    host.innerHTML = '';
    if (!listing.alarms.length) {
      const p = document.createElement('p');
      p.className = 'card-note';
      p.textContent = 'Nothing scheduled.';
      host.appendChild(p);
    } else {
      for (const alarm of listing.alarms) host.appendChild(alarmRow(alarm, reload));
    }
    $('alarm-limit-note').textContent = listing.one_alarm_limit;

    if (!TICKER) TICKER = setInterval(retickCountdowns, 1000);
  } catch (err) {
    host.innerHTML = '';
    banner('error', 'Could not load alarms', err.message);
  }
}

async function createAlarm(payload) {
  try {
    const res = await api('/api/alarms', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (res.armed) {
      banner('info', 'Alarm set on the strap', res.message);
    } else if (res.queued) {
      // Waiting its turn behind an earlier alarm is normal, not a failure.
      banner('info', 'Scheduled — queued behind an earlier alarm', res.message);
    } else {
      // The one that matters: scheduled here but the strap never took it.
      banner('error', 'Scheduled, but NOT set on the strap', res.message);
    }
  } catch (err) {
    banner('error', 'Could not schedule that', err.message);
  }
  loadAlarms();
}

export function initAlarms() {
  const quick = $('quick-timers');
  for (const timer of QUICK_TIMERS) {
    const btn = document.createElement('button');
    btn.className = 'btn';
    btn.type = 'button';
    btn.textContent = timer.label;
    btn.addEventListener('click', () => createAlarm({
      in_seconds: timer.seconds, kind: 'timer', label: `${timer.label} timer`,
    }));
    quick.appendChild(btn);
  }

  $('alarm-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const value = $('alarm-time').value;      // "HH:MM"
    if (!value) return;
    const [h, m] = value.split(':').map(Number);
    const when = new Date();
    when.setHours(h, m, 0, 0);
    // A time earlier than now means tomorrow — the obvious reading of "07:00"
    // typed at midnight.
    if (when.getTime() <= Date.now()) when.setDate(when.getDate() + 1);

    createAlarm({
      fire_at: Math.floor(when.getTime() / 1000),
      kind: 'alarm',
      label: $('alarm-label').value.trim() || null,
    });
    $('alarm-label').value = '';
  });

  $('strap-test-btn').addEventListener('click', async () => {
    const btn = $('strap-test-btn');
    btn.disabled = true;
    btn.textContent = 'sending…';
    try {
      const res = await api('/api/strap/test?seconds=20', { method: 'POST' });
      banner(res.ok ? 'info' : 'error',
        res.ok ? `Should buzz at ${res.expect_buzz_at}` : 'Test failed',
        res.ok ? res.reminder : res.message);
    } catch (err) {
      banner('error', 'Test failed', err.message);
    }
    btn.textContent = 'Test buzz (20s)';
    btn.disabled = false;
    loadAlarms();
  });
}
