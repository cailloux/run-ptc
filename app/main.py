import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db, exclusions
from app.api import default_city_client, default_intervals_client, router
from app.config import ROOT
from app.jobs import mark_interrupted

log = logging.getLogger(__name__)


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
# Tests swap these for fakes so POST /sync and /refresh never touch the network.
app.state.intervals_client_factory = default_intervals_client
app.state.city_client_factory = default_city_client
app.include_router(router)
# Mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="static")
