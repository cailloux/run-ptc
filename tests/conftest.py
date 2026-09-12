import os

import pytest

from app import db


@pytest.fixture(scope="session")
def db_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.exit("TEST_DATABASE_URL is not set; run scripts/kirk-test.sh", returncode=2)
    with db.connect(url) as conn:
        db.migrate(conn)
    return url


@pytest.fixture
def conn(db_url):
    with db.connect(db_url) as c:
        # On tiny, never-analyzed fixture tables the planner's cost estimates
        # trip the JIT threshold and every query pays to compile (16 s suite vs
        # 4 s). On real data JIT makes no measurable difference either way.
        c.execute("SET jit = off")
        c.execute("TRUNCATE segment, node, source_duplicate, activity, activity_piece, job_run,"
                  " route_edge, route_vertex RESTART IDENTITY CASCADE")
        yield c
