// Helpers shared by the map (map.js, route.js), the status page (status.js),
// and the share pages (share.js).

const METERS_PER_MILE = 1609.344;

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

// A design token from tokens.css, e.g. token('--map-route-new').
function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// A Leaflet line style from the tokens: colour --map-<name>, weight --map-w-<name>.
function lineStyle(name, extra = {}) {
  return { color: token(`--map-${name}`), weight: parseFloat(token(`--map-w-${name}`)), opacity: 1, ...extra };
}

// The coverage lines, one style per network state (docs/design/map-styles.md).
// Finished ground is gray; colour means unfinished.
function coverageStyles() {
  return {
    cartpath: {
      complete: lineStyle('cartpath-complete'),
      run: lineStyle('cartpath-run'),
      not_run: lineStyle('cartpath-not-run'),
    },
    road: {
      run: lineStyle('road-run'),
      not_run: lineStyle('road-not-run'),
    },
    excluded: lineStyle('excluded', { dashArray: '4 6' }),
    uncounted: lineStyle('uncounted'),
  };
}
