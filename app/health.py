"""Data health: is each data source current, and what should be sent about it.

Two sources, each fed by jobs in job_run:
  sync: Intervals.icu runs (the sync job)
  city: the city's layers (refresh and import)

A source is failing when its latest finished attempt failed, stale when its
last success is older than stale_after_hours, and ok otherwise. The app can't
reach Unraid's notify, so alerts() only decides what to send; the nightly User
Script sends it and calls mark_sent(). An alert goes out once per episode, and
a "recovered" notice once the episode ends.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from app.config import Settings
from app.jobs import job_running
from app.sync import EASTERN

SOURCES = {
    "sync": ("Intervals sync", ("sync",)),
    "city": ("City data", ("refresh", "import")),
}

# Separators for the alerts command's output, read by the nightly script.
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


@dataclass
class Health:
    source: str
    label: str
    state: str                    # ok | failing | stale
    last_ok: dict | None          # job, finished_at, headline
    latest: dict | None           # the latest finished attempt
    running: dict | None          # the job running now: id, job, trigger, started_at
    advice: str | None
    stale_hours: float

    def problem(self) -> str | None:
        """One line on what's wrong, or None when ok."""
        if self.state == "failing":
            return f"{self.label} failing: {brief(self.latest['error'])}"
        if self.state == "stale":
            if self.last_ok is None:
                return f"{self.label} has never succeeded"
            return f"{self.label} stale: no success since {eastern(self.last_ok['finished_at'])}"
        return None

    def as_dict(self) -> dict:
        return {"state": self.state, "label": self.label, "problem": self.problem(),
                "advice": self.advice, "running": self.running is not None, "running_job": self.running,
                "last_ok": self.last_ok, "latest": self.latest}


def first_line(text: str | None) -> str:
    return (text or "").split("\n")[0]


def brief(error: str | None) -> str:
    """An error's first line without the request URL httpx appends."""
    return re.sub(r" for url '[^']*'", "", first_line(error))


def shorten(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def eastern(ts: datetime) -> str:
    local = ts.astimezone(EASTERN)
    return f"{local:%b} {local.day}, {local:%-I:%M %p}"


def _advice(source: str, state: str, error: str | None) -> str | None:
    if state == "stale":
        return ("Check that the run-ptc-nightly User Script ran (its log is in User Scripts) "
                "and that the run-ptc container is up.")
    if state != "failing":
        return None
    error = error or ""
    if error.startswith("interrupted"):
        return "The app restarted in the middle of the job. Run it again from the status page."
    if source == "sync":
        if "401" in error or "403" in error:
            return ("Intervals rejected the credentials. Check INTERVALS_API_KEY and "
                    "INTERVALS_ATHLETE_ID in the run-ptc container's settings.")
        if "must be set" in error:
            return "Set INTERVALS_API_KEY and INTERVALS_ATHLETE_ID in the run-ptc container's settings."
        return ("Intervals.icu may be down or unreachable. The nightly sync tries again, "
                "or use Sync now on the status page.")
    return ("The city's GIS server may be down; it has returned empty or partial results "
            "before. The nightly refresh tries again, or use Check now on the status page.")


def source_health(conn: psycopg.Connection, settings: Settings, source: str,
                  now: datetime) -> Health:
    label, jobs = SOURCES[source]
    latest = conn.execute("""
        SELECT id, job, trigger, status, started_at, finished_at, error FROM job_run
        WHERE job = ANY(%s) AND status <> 'running'
        ORDER BY finished_at DESC, id DESC LIMIT 1
    """, (list(jobs),)).fetchone()
    last_ok = conn.execute("""
        SELECT job, finished_at, summary FROM job_run
        WHERE job = ANY(%s) AND status = 'ok'
        ORDER BY finished_at DESC, id DESC LIMIT 1
    """, (list(jobs),)).fetchone()
    if latest and latest[3] == "failed":
        state = "failing"
    elif last_ok is None or now - last_ok[1] > timedelta(hours=settings.stale_after_hours):
        state = "stale"
    else:
        state = "ok"
    cols = ("id", "job", "trigger", "status", "started_at", "finished_at", "error")
    return Health(
        source=source,
        label=label,
        state=state,
        last_ok={"job": last_ok[0], "finished_at": last_ok[1], "headline": first_line(last_ok[2])}
                if last_ok else None,
        latest=dict(zip(cols, latest)) if latest else None,
        running=_running_job(conn, jobs),
        advice=_advice(source, state, latest[6] if latest else None),
        stale_hours=settings.stale_after_hours,
    )


def _running_job(conn: psycopg.Connection, jobs: tuple[str, ...]) -> dict | None:
    # A 'running' row counts only while the lock is held; a crash can leave
    # one behind until the app restarts and marks it interrupted.
    if not job_running(conn):
        return None
    row = conn.execute("""
        SELECT id, job, trigger, started_at FROM job_run
        WHERE job = ANY(%s) AND status = 'running' ORDER BY started_at DESC LIMIT 1
    """, (list(jobs),)).fetchone()
    return dict(zip(("id", "job", "trigger", "started_at"), row)) if row else None


def all_health(conn: psycopg.Connection, settings: Settings, now: datetime) -> dict[str, Health]:
    return {source: source_health(conn, settings, source, now) for source in SOURCES}


def nightly_last_run(conn: psycopg.Connection) -> datetime | None:
    """When the nightly script last started a job."""
    return conn.execute(
        "SELECT max(started_at) FROM job_run WHERE trigger = 'schedule'").fetchone()[0]


# ---- alerts ------------------------------------------------------------------


@dataclass
class Notice:
    key: str            # "<episode id>:alert" or "<episode id>:recovered", for mark_sent
    severity: str       # alert | normal (notify's -i)
    subject: str
    description: str
    body: str

    def record(self) -> str:
        return FIELD_SEP.join((self.key, self.severity, self.subject, self.description, self.body))


def _alert(episode_id: int, h: Health, since: datetime) -> Notice:
    if h.state == "failing":
        lines = [f"{h.label} is failing."]
    else:
        lines = [f"{h.label} hasn't succeeded in over {h.stale_hours:g} hours."]
    lines += ["", f"Since: {eastern(since)}"]
    if h.last_ok:
        lines.append(f"Last success: {eastern(h.last_ok['finished_at'])} ({h.last_ok['headline']})")
    if h.latest and h.latest["error"]:
        lines += ["", f"Last error ({h.latest['job']} job {h.latest['id']}):", h.latest["error"]]
    lines += ["", f"What to do: {h.advice}", "",
              "You'll get one more notice when it recovers, and nothing in between."]
    return Notice(f"{episode_id}:alert", "alert", f"Run PTC: {h.label.lower()} {h.state}",
                  shorten(h.problem()), "\n".join(lines))


def _recovered(episode_id: int, h: Health, since: datetime) -> Notice:
    ok = h.last_ok
    return Notice(
        f"{episode_id}:recovered", "normal", f"Run PTC: {h.label.lower()} recovered",
        shorten(f"{h.label} is working again: {ok['headline']}"),
        f"{h.label} succeeded at {eastern(ok['finished_at'])}, after a problem that began "
        f"{eastern(since)}.\n\n{ok['job']}: {ok['headline']}")


def alerts(conn: psycopg.Connection, settings: Settings, now: datetime) -> list[Notice]:
    """Open and close episodes to match current health; return what needs sending.

    Nothing is marked sent here. The script calls mark_sent() after notify
    succeeds, so a notice that fails to send comes back the next night.
    """
    notices = []
    for source, h in all_health(conn, settings, now).items():
        row = conn.execute("""
            SELECT id, since, alerted_at, recovered_at FROM alert_episode
            WHERE source = %s AND closed_at IS NULL
        """, (source,)).fetchone()
        if h.state != "ok":
            if row is None:
                episode_id = conn.execute("""
                    INSERT INTO alert_episode (source, problem, detail, since)
                    VALUES (%s, %s, %s, %s) RETURNING id
                """, (source, h.state, h.problem(), now)).fetchone()[0]
                notices.append(_alert(episode_id, h, now))
                continue
            episode_id, since, alerted_at, recovered_at = row
            if recovered_at is not None:    # broke again before the recovery notice went out
                conn.execute("UPDATE alert_episode SET recovered_at = NULL WHERE id = %s", (episode_id,))
            if alerted_at is None:          # the alert never went out
                notices.append(_alert(episode_id, h, since))
        elif row is not None:
            episode_id, since, alerted_at, _ = row
            if alerted_at is None:          # recovered before anyone was told
                conn.execute("UPDATE alert_episode SET recovered_at = %s, closed_at = %s WHERE id = %s",
                             (now, now, episode_id))
            else:
                conn.execute("UPDATE alert_episode SET recovered_at = coalesce(recovered_at, %s)"
                             " WHERE id = %s", (now, episode_id))
                notices.append(_recovered(episode_id, h, since))
    return notices


def mark_sent(conn: psycopg.Connection, key: str) -> None:
    """The script sent a notice: an alert is now on record; a recovery closes the episode."""
    episode_id, kind = key.split(":")
    if kind == "alert":
        conn.execute("UPDATE alert_episode SET alerted_at = now() WHERE id = %s AND alerted_at IS NULL",
                     (int(episode_id),))
    elif kind == "recovered":
        conn.execute("UPDATE alert_episode SET closed_at = now() WHERE id = %s AND closed_at IS NULL",
                     (int(episode_id),))
    else:
        raise ValueError(f"unknown notice key {key!r}")


def recent_episodes(conn: psycopg.Connection, limit: int = 10) -> list[dict]:
    cols = ("id", "source", "problem", "detail", "since", "alerted_at", "recovered_at", "closed_at")
    return [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM alert_episode ORDER BY since DESC, id DESC LIMIT %s", (limit,))]
