// Helpers shared by the map (map.js, route.js) and the status page (status.js).

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function formatEastern(iso, opts = { dateStyle: 'medium', timeStyle: 'short' }) {
  return new Date(iso).toLocaleString('en-US', { timeZone: 'America/New_York', ...opts });
}

async function getJson(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail ?? `${url}: ${resp.status}`);
  }
  return resp.json();
}

// An error's first line, without the request URL httpx appends (app/health.py brief()).
function briefError(error) {
  return (error ?? '').split('\n')[0].replace(/ for url '[^']*'/, '');
}
