import pytest

from app import db
from app.jobs import JOB_LOCK, JobBusy, job_running, last_runs, mark_interrupted, start_job


@pytest.fixture
def connect(db_url):
    return lambda: db.connect(db_url)


@pytest.fixture
def other(db_url):
    """A second session, standing in for another process holding the lock."""
    with db.connect(db_url) as c:
        yield c


def job_row(conn, job_id):
    return conn.execute(
        "SELECT status, summary, error, finished_at IS NOT NULL FROM job_run WHERE id = %s", (job_id,)
    ).fetchone()


def test_success_is_recorded_and_the_lock_released(conn, connect):
    job = start_job("sync", "cli", connect=connect)
    assert job.run(lambda c: ["2 new city runs", "detail"]) == ["2 new city runs", "detail"]
    assert job_row(conn, job.id) == ("ok", "2 new city runs\ndetail", None, True)
    assert not job_running(conn)


def test_failure_is_recorded_and_reraised(conn, connect):
    job = start_job("sync", "cli", connect=connect)

    def boom(c):
        raise ValueError("Intervals said no")

    with pytest.raises(ValueError):
        job.run(boom)
    assert job_row(conn, job.id) == ("failed", None, "ValueError: Intervals said no", True)
    assert not job_running(conn)


def test_background_failures_can_be_swallowed(conn, connect):
    job = start_job("sync", "button", connect=connect)
    assert job.run(lambda c: 1 / 0, reraise=False) == []
    assert job_row(conn, job.id)[0] == "failed"


def test_a_held_lock_makes_new_jobs_busy(conn, other, connect):
    other.execute("SELECT pg_advisory_lock(%s)", (JOB_LOCK,))
    assert job_running(conn)
    with pytest.raises(JobBusy):
        start_job("recompute", "cli", connect=connect)
    assert conn.execute("SELECT count(*) FROM job_run").fetchone()[0] == 0
    other.execute("SELECT pg_advisory_unlock(%s)", (JOB_LOCK,))
    assert not job_running(conn)


def test_interrupted_jobs_are_marked_only_when_no_one_holds_the_lock(conn, other):
    job_id = conn.execute(
        "INSERT INTO job_run (job, trigger) VALUES ('sync', 'button') RETURNING id").fetchone()[0]
    other.execute("SELECT pg_advisory_lock(%s)", (JOB_LOCK,))
    assert mark_interrupted(conn) == 0          # still running somewhere
    assert job_row(conn, job_id)[0] == "running"

    other.execute("SELECT pg_advisory_unlock(%s)", (JOB_LOCK,))
    assert mark_interrupted(conn) == 1
    status, _, error, finished = job_row(conn, job_id)
    assert (status, finished) == ("failed", True) and error.startswith("interrupted")


def test_last_runs_reports_the_latest_attempt_and_the_last_success(conn, connect):
    start_job("sync", "cli", connect=connect).run(lambda c: ["1 new city run", "detail"])
    start_job("sync", "button", connect=connect).run(lambda c: 1 / 0, reraise=False)
    status = last_runs(conn, "sync")
    assert status["running"] is False
    assert (status["latest"]["status"], status["latest"]["trigger"]) == ("failed", "button")
    assert status["last_ok"]["headline"] == "1 new city run"
