import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app import db, exclusions
from app.api import default_city_client, default_intervals_client, router
from app.config import ROOT
from app.jobs import mark_interrupted

log = logging.getLogger(__name__)

# The Authelia session cookie's name, as set on ptc.grommet.co. Its mere
# presence is enough here -- it's not treated as proof of a valid session
# (Authelia/Traefik already enforce that on every route they gate), only as
# "this browser has logged in," which is what should defeat public caching.
SESSION_COOKIE = "authelia_session"
PUBLIC_CACHE_SECONDS = 24 * 60 * 60   # coverage changes at most once a day
# Only these get cached for an anonymous visitor. Deliberately an allowlist,
# not "everything but the cookie": if the external Cloudflare/Traefik rule
# is ever widened by mistake, that's a routing bug, not also a caching one.
PUBLIC_CACHE_PATHS = frozenset({
    "/left", "/left.html", "/progress", "/progress.html", "/stats", "/network",
    "/share.js", "/common.js", "/tokens.css", "/style.css",
    "/favicon.svg", "/apple-touch-icon.png",
})


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Cache-Control tuned in code, not in Cloudflare: a long public TTL for
    the allowlisted public paths when the visitor hasn't logged in, never
    cached at all otherwise, so a logged-in session always sees live data."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if (request.method in ("GET", "HEAD") and request.url.path in PUBLIC_CACHE_PATHS
                and SESSION_COOKIE not in request.cookies):
            response.headers["Cache-Control"] = f"public, max-age={PUBLIC_CACHE_SECONDS}"
        else:
            response.headers["Cache-Control"] = "private, no-store"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.connect_with_retry() as conn:
        db.migrate(conn)
        if interrupted := mark_interrupted(conn):
            log.warning("marked %d job(s) interrupted by a restart", interrupted)
        # A bad exclusions file shouldn't take the map down; keep the last
        # applied state and say so.
        try:
            exclusions.apply(conn, exclusions.load())
        except exclusions.ExclusionError as e:
            log.error("exclusions.yaml not applied: %s", e)
    yield


app = FastAPI(title="Run PTC", lifespan=lifespan)
# /network is ~1.4-1.8 MB of GeoJSON per layer and compresses about 6x. Level 5
# takes ~25 ms; the default 9 takes 3-4x as long for ~3% less.
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
app.add_middleware(CacheControlMiddleware)
# Tests swap these for fakes so POST /sync and /refresh never touch the network.
app.state.intervals_client_factory = default_intervals_client
app.state.city_client_factory = default_city_client
app.include_router(router)
# Mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="static")
