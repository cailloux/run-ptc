"""The nightly User Script template, run with fake `docker` and `notify`."""

import os
import subprocess
import textwrap

import pytest

from app.config import ROOT

SCRIPT = ROOT / "deploy" / "unraid" / "user-scripts" / "run-ptc-nightly" / "script"


RS, US = "\x1e", "\x1f"


@pytest.fixture
def fakes(tmp_path):
    # docker inspect: prints FAKE_RUNNING (default "true").
    # docker exec ... alerts: prints FAKE_ALERTS_OUT; alerts --sent KEY logs KEY.
    # docker exec ... JOB: answers from FAKE_<JOB>_OUT and FAKE_<JOB>_CODE.
    # notify: one line per call, arguments joined by |; exits FAKE_NOTIFY_CODE.
    docker = tmp_path / "docker"
    docker.write_text(textwrap.dedent("""\
        #!/bin/bash
        if [ "$1" = inspect ]; then echo "${FAKE_RUNNING:-true}"; exit 0; fi
        if [ "${@: -2:1}" = --sent ]; then echo "${@: -1}" >> "$SENT_LOG"; exit 0; fi
        job="${@: -1}"
        if [ "$job" = alerts ]; then printf '%s' "${FAKE_ALERTS_OUT:-}"; exit "${FAKE_ALERTS_CODE:-0}"; fi
        out="FAKE_${job^^}_OUT"; code="FAKE_${job^^}_CODE"
        printf '%b\\n' "${!out:-ok}"
        exit "${!code:-0}"
    """))
    notify = tmp_path / "notify"
    notify.write_text('#!/bin/bash\nargs=("${@//$\'\\n\'/ }"); IFS="|"; echo "${args[*]}" >> "$NOTIFY_LOG"\n'
                      'exit "${FAKE_NOTIFY_CODE:-0}"\n')
    for f in (docker, notify):
        f.chmod(0o755)
    return tmp_path


def run(fakes, **env):
    """Returns (exit code, stdout, notify calls, alerts marked sent)."""
    log, sent = fakes / "notify.log", fakes / "sent.log"
    log.touch()
    sent.touch()
    proc = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True,
        env={"PATH": f"{fakes}:{os.environ['PATH']}", "NOTIFY": str(fakes / "notify"),
             "NOTIFY_LOG": str(log), "SENT_LOG": str(sent), **env})
    return proc.returncode, proc.stdout, log.read_text().splitlines(), sent.read_text().splitlines()


def record(key, severity, subject, description, body):
    return US.join((key, severity, subject, description, body)) + RS


def test_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_a_quiet_night_sends_nothing(fakes):
    code, out, sent, _ = run(fakes, FAKE_REFRESH_OUT="city data unchanged (cart paths last edited 2026-04-21)")
    assert code == 0 and sent == []
    assert "=== sync" in out and "=== refresh" in out and "=== alerts" in out


def test_new_city_data_sends_a_normal_notice_with_the_headline(fakes):
    code, _, sent, _ = run(fakes, APP_URL="http://kirk:8010",
                           FAKE_REFRESH_OUT="cart paths: 2 added, 0 changed, 0 removed; roads: unchanged\\nimpact")
    assert code == 0
    [notice] = sent
    assert "-i|normal|-s|Run PTC: city data updated|-d|cart paths: 2 added" in notice
    assert notice.endswith("http://kirk:8010/#changes")


def test_notices_from_the_app_are_sent_then_marked_sent(fakes):
    alerts = (record("4:alert", "alert", "Run PTC: intervals sync failing", "Intervals sync failing: 401",
                     "Intervals sync failing: 401.\n\nWhat to do: check the key")
              + record("3:recovered", "normal", "Run PTC: city data recovered", "City data is working again", "ok"))
    code, _, sent, marked = run(fakes, FAKE_ALERTS_OUT=alerts, APP_URL="http://kirk:8010")
    assert code == 0
    assert sent[0].startswith("-e|Run PTC|-i|alert|-s|Run PTC: intervals sync failing|"
                              "-d|Intervals sync failing: 401|-m|Intervals sync failing: 401.")
    assert sent[0].endswith("What to do: check the key  http://kirk:8010/status.html")
    assert sent[1].startswith("-e|Run PTC|-i|normal|-s|Run PTC: city data recovered|")
    assert marked == ["4:alert", "3:recovered"]


def test_a_notice_that_fails_to_send_isnt_marked_sent(fakes):
    code, out, sent, marked = run(fakes, FAKE_NOTIFY_CODE="1",
                                  FAKE_ALERTS_OUT=record("4:alert", "alert", "s", "d", "b"))
    assert code == 1 and len(sent) == 1 and marked == []
    assert "offer this notice again tomorrow" in out


def test_a_failure_the_app_recorded_is_left_to_the_alerts_step(fakes):
    code, out, sent, _ = run(
        fakes, FAKE_SYNC_CODE="1",
        FAKE_SYNC_OUT="ERROR app.jobs: job 16 failed\\nTraceback (most recent call last):\\n"
                      "sync failed (job 16): HTTPStatusError: Client error '401 Unauthorized'")
    assert code == 1 and sent == []
    assert "=== refresh" in out


def test_a_failure_outside_a_job_alerts_directly(fakes):
    code, _, sent, _ = run(fakes, FAKE_REFRESH_CODE="1",
                           FAKE_REFRESH_OUT="exclusions.yaml: 12062: no such cart path")
    assert code == 1
    [alert] = sent
    assert "-i|alert|-s|Run PTC: nightly refresh failed|-d|exclusions.yaml: 12062: no such cart path|" in alert
    assert "before the app could record it" in alert


def test_a_long_cause_is_shortened_in_the_description_but_kept_in_the_body(fakes):
    cause = "RuntimeError: " + "x" * 200
    _, _, sent, _ = run(fakes, FAKE_SYNC_CODE="1", FAKE_SYNC_OUT=cause)
    [alert] = sent
    description = alert.split("|-d|")[1].split("|-m|")[0]
    assert len(description) == 160 and description.endswith("...")
    assert f"Cause: {cause}" in alert


def test_a_stopped_container_alerts_and_skips_everything(fakes):
    code, out, sent, _ = run(fakes, CONTAINER="run-ptc-gone", FAKE_RUNNING="Error: No such object: run-ptc-gone")
    assert code == 1 and "=== sync" not in out
    [alert] = sent
    assert "-s|Run PTC: nightly jobs didn't run|-d|Container run-ptc-gone isn't running or doesn't exist" in alert
    assert "Docker said: Error: No such object: run-ptc-gone" in alert


def test_a_busy_job_sends_nothing(fakes):
    code, _, sent, _ = run(fakes, FAKE_REFRESH_CODE="75", FAKE_REFRESH_OUT="another job is running")
    assert code == 1 and sent == []


def test_a_failed_alerts_command_alerts(fakes):
    code, _, sent, _ = run(fakes, FAKE_ALERTS_CODE="1")
    assert code == 1
    [alert] = sent
    assert "-s|Run PTC: health check failed|" in alert


def test_test_alert_mode(fakes):
    code, out, sent, _ = run(fakes, TEST_ALERT="1")
    assert code == 0 and "=== sync" not in out
    assert sent == ["-e|Run PTC|-i|alert|-s|Run PTC: test alert|-d|Nightly job alerts will arrive like this."]
