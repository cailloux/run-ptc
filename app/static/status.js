// The status page: each data source's health, the city layers, alerts, and
// recent jobs, from GET /status. Polls while a job runs.

const POLL_MS = 3000;
const BADGE = {
  ok: ['✓', 'Current'],
  stale: ['!', 'Stale'],
  failing: ['✕', 'Failing'],
  running: ['⟳', 'Running'],
};
const SOURCE_BUTTON = {
  sync: { label: 'Sync now', url: 'sync' },
  city: { label: 'Check city now', url: 'refresh' },
};
const TRIGGER = { button: 'manual', schedule: 'nightly', cli: 'command line' };
const JOB_STATUS = { ok: ['✓', 'ok'], failed: ['✕', 'failed'], running: ['⟳', 'running'] };
const LAYER_LABEL = { cartpath: 'Cart paths', road: 'Roads' };
const SHORT = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };

let poll = null;
const messages = {};   // per source: the last button error, kept across renders

const $ = (id) => document.getElementById(id);

function when(iso, opts = SHORT) {
  return iso ? escapeHtml(formatEastern(iso, opts)) : '—';
}

function day(iso) {
  return when(iso, { dateStyle: 'medium' });
}

function trigger(t) {
  return escapeHtml(TRIGGER[t] ?? t);
}

function ago(iso, now) {
  const s = Math.max(0, Math.round((new Date(now) - new Date(iso)) / 1000));
  return s < 90 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
}

function badge(state) {
  const [icon, label] = BADGE[state];
  return `<span class="badge ${state}"><span class="icon" aria-hidden="true">${icon}</span>${label}</span>`;
}

function duration(job) {
  if (!job.finished_at) return '—';
  const s = (new Date(job.finished_at) - new Date(job.started_at)) / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

function dl(rows) {
  return `<dl>${rows.filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;
}

// Setting names in advice read as code.
function advice(text) {
  return escapeHtml(text ?? '').replace(/\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/g, (m) => `<code>${m}</code>`);
}

function renderSource(name, src, now, extraRows = []) {
  const state = src.running ? 'running' : src.state;
  const card = $(`${name}-card`);
  card.className = `card ${state === 'ok' ? '' : state}`;
  const b = SOURCE_BUTTON[name];
  const latest = src.latest;
  const job = src.running_job;
  const rows = [
    job && ['Started', `${when(job.started_at)} · ${escapeHtml(job.job)} (${trigger(job.trigger)}) · ${ago(job.started_at, now)}`],
    ['Last success', src.last_ok ? `${when(src.last_ok.finished_at)} · ${escapeHtml(src.last_ok.headline)}` : 'never'],
    !job && latest && ['Last attempt', `${when(latest.finished_at)} · ${escapeHtml(latest.job)} `
      + `(${trigger(latest.trigger)}) · ${escapeHtml(latest.status)}`],
    src.state === 'failing' && ['Error', `<div class="error-block">${escapeHtml(latest.error ?? '')}</div>`],
    src.state !== 'ok' && ['What to do', advice(src.advice)],
    ...extraRows,
  ];
  const message = messages[name] ? `<div class="message">✕ ${escapeHtml(messages[name])}</div>` : '';
  card.innerHTML = `<h2>${escapeHtml(src.label)} ${badge(state)}`
    + `<button class="btn" data-source="${name}" ${src.running ? 'disabled' : ''}>${b.label}</button></h2>`
    + (src.running ? '<div class="progress"><i></i></div>' : '')
    + dl(rows) + message;
}

function renderLayers(city) {
  const rows = Object.entries(city.layers).map(([layer, l]) => `<tr>
      <td>${LAYER_LABEL[layer]}</td>
      <td class="num">${l.city_features?.toLocaleString('en-US') ?? '—'}</td>
      <td>${day(l.city_last_edited)}</td>
      <td>${when(l.imported_at)}</td>
      <td class="num">${l.counted.toLocaleString('en-US')} (${l.counted_mi.toFixed(2)} mi)</td>
      <td class="num">${l.excluded}</td>
    </tr>`).join('');
  const change = city.last_change;
  const changeHtml = change
    ? `<p>Latest city change, ${day(change.changed_at)}: <b>${change.added} added</b>, `
      + `<b>${change.changed} changed</b> · <a href="./#changes">Show on the map</a></p>`
      + (change.report ? `<details><summary>Change report (${escapeHtml(change.job)} job ${change.job_id})</summary>`
        + `<pre>${escapeHtml(change.report)}</pre></details>` : '')
    : '<div class="empty">No city changes since the first import.</div>';
  $('layers-card').innerHTML = `<h2>City layers</h2>
    <table>
      <tr><th>Layer</th><th class="num">City features</th><th>City last edited</th>
          <th>Last imported</th><th class="num">Counted</th><th class="num">Excluded</th></tr>
      ${rows}
    </table>${changeHtml}`;
}

function renderAlerts(s) {
  const nightly = s.nightly.last_run
    ? `${when(s.nightly.last_run)}${s.nightly.overdue
      ? ` <span class="error">(more than ${s.stale_after_hours} h ago)</span>` : ''}`
    : 'never (the nightly User Script hasn\'t run against this app)';
  const episodes = s.episodes.map((e) => {
    const recovered = e.closed_at && e.recovered_at ? when(e.recovered_at)
      : (e.recovered_at ? 'yes, notice pending' : '<span class="error">● ongoing</span>');
    return `<tr>
      <td>${e.source === 'sync' ? 'Intervals sync' : 'City data'}</td>
      <td class="wrap">${escapeHtml(briefError(e.detail ?? e.problem))}</td>
      <td>${when(e.since)}</td>
      <td>${e.alerted_at ? when(e.alerted_at) : 'not yet'}</td>
      <td>${recovered}</td>
    </tr>`;
  }).join('');
  $('alerts-card').innerHTML = '<h2>Alerts</h2>' + dl([
    ['Nightly script last ran', nightly],
    ['Last alert sent', s.last_alert_sent ? when(s.last_alert_sent) : 'none'],
  ])
    + (episodes
      ? `<table id="episodes"><tr><th>Source</th><th>Problem</th><th>Since</th><th>Alert sent</th><th>Recovered</th></tr>${episodes}</table>`
      : '<div class="empty">No problems on record.</div>')
    + '<p class="note">Alerts come from the run-ptc-nightly User Script through Unraid notifications: '
    + 'one when a source starts failing or goes stale, one when it recovers. To test the path, '
    + 'set <code>TEST_ALERT=1</code> in the script\'s settings, run it, then set it back to 0.</p>';
}

function renderJobs(jobs) {
  $('jobs').innerHTML = '<tr><th>Started</th><th>Job</th><th>Trigger</th>'
    + '<th class="num">Took</th><th>Status</th><th>Result</th></tr>'
    + jobs.map((j) => {
      // A failure's summary line drops the request URL; expanding shows the whole error.
      let head;
      let rest;
      if (j.status === 'failed') {
        head = briefError(j.error);
        rest = head === j.error ? '' : j.error;
      } else if (j.status === 'running') {
        head = '';
        rest = '';
      } else {
        [head, ...rest] = (j.summary ?? '').split('\n');
        rest = rest.join('\n');
      }
      const result = rest
        ? `<details><summary>${escapeHtml(head)}</summary><pre>${escapeHtml(rest)}</pre></details>`
        : escapeHtml(head);
      const [icon, label] = JOB_STATUS[j.status];
      return `<tr${j.status === 'running' ? ' class="live"' : ''}>
        <td>${when(j.started_at)}</td><td>${escapeHtml(j.job)}</td><td>${trigger(j.trigger)}</td>
        <td class="num">${duration(j)}</td>
        <td class="status-${j.status}">${icon} ${label}</td>
        <td class="result${j.status === 'failed' ? ' error' : ''}">${result}</td></tr>`;
    }).join('');
}

async function load() {
  const s = await getJson('status');
  const sync = s.sources.sync;
  const c = sync.counts;
  renderSource('sync', sync, s.now, [
    ['Runs', `${c.city} in the city, ${c.outside} outside it, ${c.no_gps} without GPS`],
    ['Latest run', day(c.latest_run)],
  ]);
  renderSource('city', s.sources.city, s.now);
  renderLayers(s.sources.city);
  renderAlerts(s);
  renderJobs(s.jobs);
  $('updated').textContent = `Updated ${formatEastern(s.now)}`;
  const running = Object.values(s.sources).some((src) => src.running);
  if (running && !poll) poll = setInterval(() => load().catch(showError), POLL_MS);
  if (!running && poll) {
    clearInterval(poll);
    poll = null;
  }
}

function showError(err) {
  $('updated').innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
}

document.querySelector('main').addEventListener('click', async (e) => {
  const name = e.target.dataset?.source;
  if (!name) return;
  e.target.disabled = true;
  delete messages[name];
  try {
    await getJson(SOURCE_BUTTON[name].url, { method: 'POST' });
  } catch (err) {   // 409 (another job running) or 503 (missing credentials or bad exclusions)
    messages[name] = err.message;
  }
  await load().catch(showError);
});

load().catch(showError);
