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

// OpenStreetMap's standard tiles, used within the OSMF tile policy
// (https://operations.osmfoundation.org/policies/tiles/): visible
// attribution, the browser's normal Referer, no bulk or prefetching. They're
// shown in grayscale (style.css) so OSM's own colours don't compete with
// the coverage lines, which are judged against this gray.
function basemap(map) {
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    className: 'basemap',
    maxNativeZoom: 19,
    maxZoom: 20,
  }).addTo(map);
}
