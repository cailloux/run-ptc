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
    # db.connect() turns JIT off, which also keeps the suite fast.
    with db.connect(db_url) as c:
        c.execute("TRUNCATE segment, node, source_duplicate, activity, activity_piece, job_run,"
                  " route_edge, route_vertex, source_signature, alert_episode RESTART IDENTITY CASCADE")
        # network keeps the seeded ptc row stable (not in the TRUNCATE list
        # above), but a test-added second network must not leak into the next.
        c.execute("DELETE FROM network WHERE slug <> 'ptc'")
        yield c
