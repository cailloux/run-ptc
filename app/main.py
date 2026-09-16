import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app import db, exclusions
from app.api import default_city_client, default_intervals_client, router
from app.config import ROOT
from app.jobs import mark_interrupted

log = logging.getLogger(__name__)

PUBLIC_CACHE_SECONDS = 24 * 60 * 60   # coverage changes at most once a day
# The vanity hostnames the share pages are reachable on. admin.runptc.com
# (behind Cloudflare Access) is deliberately not one of these, even though
# its own map view fetches the same paths below -- an admin session should
# never see a day-old cached response to its own live data.
PUBLIC_HOSTS = frozenset({
    "progress.runptc.com", "left.runptc.com", "runptc.com", "www.runptc.com",
})
# Only these paths, and only on the hosts above, get cached for a visitor.
# Deliberately two allowlists, not "everything except admin's hostname": if
# a routing rule outside this app is ever widened by mistake, that's a
# routing bug, not also a caching one.
PUBLIC_CACHE_PATHS = frozenset({
    "/left", "/left.html", "/progress", "/progress.html", "/stats", "/network",
    "/share.js", "/common.js", "/tokens.css", "/style.css",
    "/favicon.svg", "/apple-touch-icon.png",
})


class PublicHostGateMiddleware(BaseHTTPMiddleware):
    """The public vanity hostnames route straight to this container, the
    same as admin.runptc.com, but with no Cloudflare Access in front of
    them -- so unlike admin.runptc.com, nothing else stops a request to
    one of them from reaching the full admin API. Anything off the public
    path allowlist 404s outright rather than prompting a login: there's no
    legitimate reason to reach admin functionality through a hostname
    meant only for the read-only share pages, and admin.runptc.com already
    exists for that."""

    async def dispatch(self, request, call_next):
        if request.url.hostname in PUBLIC_HOSTS and request.url.path not in PUBLIC_CACHE_PATHS:
            return Response(status_code=404)
        return await call_next(request)


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Cache-Control tuned in code, not in Cloudflare: a long public TTL for
    the allowlisted public paths on the public hostnames, never cached at
    all otherwise -- so admin.runptc.com, even hitting the same paths,
    always sees live data."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if (request.method in ("GET", "HEAD") and request.url.path in PUBLIC_CACHE_PATHS
                and request.url.hostname in PUBLIC_HOSTS):
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
app.add_middleware(PublicHostGateMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
app.add_middleware(CacheControlMiddleware)
# Tests swap these for fakes so POST /sync and /refresh never touch the network.
app.state.intervals_client_factory = default_intervals_client
app.state.city_client_factory = default_city_client
app.include_router(router)
# Mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="static")
