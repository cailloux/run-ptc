"""The job log and the one lock every data job shares.

Sync, import, and recompute all change hits, so only one runs at a time,
whether started from the CLI or the map. The lock is a Postgres advisory
lock held by the job's own connection, so it's released if the process dies.
"""

import logging
from typing import Callable

import psycopg

from app import db

log = logging.getLogger(__name__)

# Arbitrary key, distinct from db.MIGRATION_LOCK.
JOB_LOCK = 7_316_241

JobFn = Callable[[psycopg.Connection], list[str]]


class JobBusy(RuntimeError):
    pass


class Job:
    """A started job: holds the lock and its job_run row until run() finishes."""

    def __init__(self, conn: psycopg.Connection, job_id: int):
        self.conn = conn
        self.id = job_id

    def run(self, fn: JobFn, *, reraise: bool = True) -> list[str]:
        """Run fn on the job's connection and record the outcome. Returns its summary lines."""
        try:
            summary = fn(self.conn)
            self.conn.execute(
                "UPDATE job_run SET status = 'ok', finished_at = now(), summary = %s WHERE id = %s",
                ("\n".join(summary), self.id),
            )
            return summary
        except Exception as e:
            log.exception("job %s failed", self.id)
            self.conn.execute(
                "UPDATE job_run SET status = 'failed', finished_at = now(), error = %s WHERE id = %s",
                (f"{type(e).__name__}: {e}", self.id),
            )
            if reraise:
                raise
            return []
        finally:
            self.conn.execute("SELECT pg_advisory_unlock(%s)", (JOB_LOCK,))
            self.conn.close()


def start_job(job: str, trigger: str, connect=db.connect) -> Job:
    """Take the job lock and record a running job. Raises JobBusy if another job holds it."""
    conn = connect()
    try:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (JOB_LOCK,)).fetchone()[0]:
            raise JobBusy("another job is running")
        job_id = conn.execute(
            "INSERT INTO job_run (job, trigger) VALUES (%s, %s) RETURNING id", (job, trigger)
        ).fetchone()[0]
    except BaseException:
        conn.close()
        raise
    return Job(conn, job_id)


def job_running(conn: psycopg.Connection) -> bool:
    return conn.execute("""
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND granted AND classid = 0 AND objid = %s AND objsubid = 1
              AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
        )
    """, (JOB_LOCK,)).fetchone()[0]


def mark_interrupted(conn: psycopg.Connection) -> int:
    """At startup: if no job holds the lock, any 'running' rows were cut off by a crash."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (JOB_LOCK,)).fetchone()[0]:
        return 0
    try:
        return conn.execute("""
            UPDATE job_run SET status = 'failed', finished_at = now(),
                   error = 'interrupted: the process running it stopped'
            WHERE status = 'running'
        """).rowcount
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (JOB_LOCK,))

