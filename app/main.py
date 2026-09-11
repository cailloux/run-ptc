import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db, exclusions
from app.api import router
from app.config import ROOT

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.connect_with_retry() as conn:
        db.migrate(conn)
        # A bad exclusions file shouldn't take the map down; keep the last
        # applied state and say so.
        try:
            exclusions.apply(conn, exclusions.load())
        except exclusions.ExclusionError as e:
            log.error("exclusions.yaml not applied: %s", e)
    yield


app = FastAPI(title="Run PTC", lifespan=lifespan)
app.include_router(router)
# Mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="static")
