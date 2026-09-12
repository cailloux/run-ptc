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
const LAYER_LABEL = { cartpath: 'Cart paths', road: 'Roads' };

let poll = null;
const messages = {};   // per source: the last button error, kept across renders

const SHORT = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };

function when(iso, opts) {
  return iso ? escapeHtml(formatEastern(iso, opts)) : '—';
}

function day(iso) {
  return when(iso, { dateStyle: 'medium' });
}

function badge(state) {
  const [icon, label] = BADGE[state];
  return `<span class="badge ${state}"><span aria-hidden="true">${icon}</span> ${label}</span>`;
}

function duration(job) {
  if (!job.finished_at) return '';
  const s = (new Date(job.finished_at) - new Date(job.started_at)) / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

function dl(rows) {
  return `<dl>${rows.filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;
}

function sourceHead(name, src) {
  const b = SOURCE_BUTTON[name];
  return `<h2>${escapeHtml(src.label)} ${badge(src.running ? 'running' : src.state)}`
    + `<button data-source="${name}" ${src.running ? 'disabled' : ''}>${b.label}</button></h2>`;
}

function sourceRows(src) {
  const latest = src.latest;
  return [
    ['Last success', src.last_ok
      ? `${when(src.last_ok.finished_at)} · ${escapeHtml(src.last_ok.headline)}` : 'never'],
    latest && ['Last attempt', `${when(latest.finished_at)} · ${escapeHtml(latest.job)} `
      + `(${escapeHtml(latest.trigger)}) · ${escapeHtml(latest.status)}`],
    src.state === 'failing' && ['Error', `<span class="error">${escapeHtml(latest.error ?? '')}</span>`],
    src.problem && ['What to do', escapeHtml(src.advice ?? '')],
  ];
}

function message(name) {
  const m = messages[name];
  return m ? `<div class="message error">${escapeHtml(m)}</div>` : '';
}

function renderSync(src) {
  const c = src.counts;
  document.getElementById('sync-card').innerHTML = sourceHead('sync', src) + dl([
    ...sourceRows(src),
    ['Runs', `${c.city} in the city, ${c.outside} outside it, ${c.no_gps} without GPS`],
    ['Latest run', day(c.latest_run)],
  ]) + message('sync');
}

function renderCity(src) {
  const rows = Object.entries(src.layers).map(([layer, l]) => `<tr>
      <td>${LAYER_LABEL[layer]}</td>
      <td class="num">${l.city_features ?? '—'}</td>
      <td>${day(l.city_last_edited)}</td>
      <td>${when(l.imported_at)}</td>
      <td class="num">${l.counted} (${l.counted_mi} mi)</td>
      <td class="num">${l.excluded}</td>
    </tr>`).join('');
  const change = src.last_change;
  const changeHtml = change
    ? `<p>Latest city change, ${day(change.changed_at)}: ${change.added} added, ${change.changed} changed · `
      + '<a href="./#changes">Show on the map</a></p>'
      + (change.report ? `<details><summary>Change report (${escapeHtml(change.job)} job ${change.job_id})</summary>`
        + `<pre>${escapeHtml(change.report)}</pre></details>` : '')
    : '<p class="note">No city changes since the first import.</p>';
  document.getElementById('city-card').innerHTML = sourceHead('city', src) + dl(sourceRows(src))
    + `<table>
        <tr><th>Layer</th><th class="num">City features</th><th>City last edited</th>
            <th>Last imported</th><th class="num">Counted</th><th class="num">Excluded</th></tr>
        ${rows}
      </table>`
    + changeHtml + message('city');
}

function renderAlerts(s) {
  const nightly = s.nightly.last_run
    ? `${when(s.nightly.last_run)}${s.nightly.overdue
      ? ` <span class="error">(more than ${s.stale_after_hours} h ago)</span>` : ''}`
    : 'never (the nightly User Script hasn\'t run against this app)';
  const episodes = s.episodes.map((e) => `<tr>
      <td>${e.source === 'sync' ? 'Intervals sync' : 'City data'}</td>
      <td class="wrap">${escapeHtml(e.detail ?? e.problem)}</td>
      <td>${when(e.since, SHORT)}</td>
      <td>${e.alerted_at ? when(e.alerted_at, SHORT) : 'not yet'}</td>
      <td>${e.closed_at ? when(e.recovered_at, SHORT) : (e.recovered_at ? 'yes, notice pending' : 'ongoing')}</td>
    </tr>`).join('');
  document.getElementById('alerts-card').innerHTML = '<h2>Alerts</h2>' + dl([
    ['Nightly script last ran', nightly],
    ['Last alert sent', s.last_alert_sent ? when(s.last_alert_sent) : 'none'],
  ])
    + (episodes ? `<table id="episodes"><tr><th>Source</th><th>Problem</th><th>Since</th><th>Alert sent</th>
        <th>Recovered</th></tr>${episodes}</table>` : '<p class="note">No problems on record.</p>')
    + '<p class="note">Alerts come from the run-ptc-nightly User Script through Unraid notifications: '
    + 'one when a source starts failing or goes stale, one when it recovers. To test the path, '
    + 'set <code>TEST_ALERT=1</code> in the script\'s settings, run it, then set it back to 0.</p>';
}

function renderJobs(jobs) {
  document.getElementById('jobs').innerHTML = '<tr><th>Started</th><th>Job</th><th>Trigger</th>'
    + '<th class="num">Took</th><th>Status</th><th>Result</th></tr>'
    + jobs.map((j) => {
      // A failure's summary line drops the request URL; expanding shows the whole error.
      let head;
      let rest;
      if (j.status === 'failed') {
        head = briefError(j.error);
        rest = head === j.error ? '' : j.error;
      } else {
        [head, ...rest] = (j.summary ?? '').split('\n');
        rest = rest.join('\n');
      }
      const result = rest
        ? `<details><summary>${escapeHtml(head)}</summary><pre>${escapeHtml(rest)}</pre></details>`
        : escapeHtml(head);
      return `<tr><td>${when(j.started_at, SHORT)}</td><td>${escapeHtml(j.job)}</td><td>${escapeHtml(j.trigger)}</td>
        <td class="num">${duration(j)}</td>
        <td${j.status === 'failed' ? ' class="error"' : ''}>${escapeHtml(j.status)}</td>
        <td${j.status === 'failed' ? ' class="error"' : ''}>${result}</td></tr>`;
    }).join('');
}

async function load() {
  const s = await getJson('status');
  renderSync(s.sources.sync);
  renderCity(s.sources.city);
  renderAlerts(s);
  renderJobs(s.jobs);
  document.getElementById('updated').textContent = `Updated ${formatEastern(s.now)}`;
  const running = Object.values(s.sources).some((src) => src.running);
  if (running && !poll) poll = setInterval(() => load().catch(showError), POLL_MS);
  if (!running && poll) {
    clearInterval(poll);
    poll = null;
  }
}

function showError(err) {
  document.getElementById('updated').innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
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
