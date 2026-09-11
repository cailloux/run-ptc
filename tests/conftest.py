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
        c.execute("TRUNCATE segment, node, source_duplicate RESTART IDENTITY CASCADE")
        yield c
