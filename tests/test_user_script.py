"""The nightly User Script template, run with fake `docker` and `notify`."""

import os
import subprocess
import textwrap

import pytest

from app.config import ROOT

SCRIPT = ROOT / "deploy" / "unraid" / "user-scripts" / "run-ptc-nightly" / "script"


@pytest.fixture
def fakes(tmp_path):
    # docker exec CONTAINER python -m app.cli JOB: answers from FAKE_<JOB>_OUT
    # and FAKE_<JOB>_CODE. notify: one line per call, arguments joined by |.
    docker = tmp_path / "docker"
    docker.write_text(textwrap.dedent("""\
        #!/bin/bash
        job="${@: -1}"
        out="FAKE_${job^^}_OUT"; code="FAKE_${job^^}_CODE"
        printf '%b\\n' "${!out:-ok}"
        exit "${!code:-0}"
    """))
    notify = tmp_path / "notify"
    notify.write_text('#!/bin/bash\nargs=("${@//$\'\\n\'/ }"); IFS="|"; echo "${args[*]}" >> "$NOTIFY_LOG"\n')
    for f in (docker, notify):
        f.chmod(0o755)
    return tmp_path


def run(fakes, **env):
    log = fakes / "notify.log"
    log.touch()
    proc = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True,
        env={"PATH": f"{fakes}:{os.environ['PATH']}", "NOTIFY": str(fakes / "notify"),
             "NOTIFY_LOG": str(log), **env})
    return proc.returncode, proc.stdout, log.read_text().splitlines()


def test_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_a_quiet_night_sends_nothing(fakes):
    code, out, sent = run(fakes, FAKE_REFRESH_OUT="city data unchanged (cart paths last edited 2026-04-21)")
    assert code == 0 and sent == []
    assert "=== sync" in out and "=== refresh" in out


def test_new_city_data_sends_a_normal_notice_with_the_headline(fakes):
    code, _, sent = run(fakes, FAKE_REFRESH_OUT="cart paths: 2 added, 0 changed, 0 removed; roads: unchanged\\nimpact")
    assert code == 0
    [notice] = sent
    assert "-i|normal|-s|Run PTC: city data updated|-d|cart paths: 2 added" in notice


def test_a_failed_job_alerts_with_the_cause_and_the_other_still_runs(fakes):
    code, out, sent = run(
        fakes, FAKE_SYNC_CODE="1",
        # The shape of a real failure: a generic log line, the traceback, then
        # the CLI's one-line summary.
        FAKE_SYNC_OUT="ERROR app.jobs: job 16 failed\\nTraceback (most recent call last):\\n"
                      "  File /app/app/jobs.py, line 37, in run\\n"
                      "httpx.HTTPStatusError: Client error '401 Unauthorized'\\n"
                      "sync failed (job 16): HTTPStatusError: Client error '401 Unauthorized'",
        FAKE_REFRESH_OUT="city data unchanged")
    assert code == 1
    [alert] = sent
    cause = "sync failed (job 16): HTTPStatusError: Client error '401 Unauthorized'"
    assert f"-i|alert|-s|Run PTC: nightly sync failed|-d|{cause}|-m|" in alert
    assert "(container run-ptc, exit code 1)" in alert
    assert f"Cause: {cause}" in alert
    assert "=== refresh" in out


def test_a_missing_container_says_so(fakes):
    code, _, sent = run(fakes, CONTAINER="run-ptc-gone", FAKE_SYNC_CODE="1",
                        FAKE_SYNC_OUT="Error response from daemon: No such container: run-ptc-gone",
                        FAKE_REFRESH_OUT="city data unchanged")
    [alert] = sent
    assert "-d|Container run-ptc-gone isn't running or doesn't exist, so nothing ran.|" in alert


def test_a_long_cause_is_shortened_in_the_description_but_kept_in_the_body(fakes):
    cause = "sync failed (job 3): HTTPStatusError: Client error for url '" + "x" * 200 + "'"
    _, _, sent = run(fakes, FAKE_SYNC_CODE="1", FAKE_SYNC_OUT=cause, FAKE_REFRESH_OUT="city data unchanged")
    [alert] = sent
    description = alert.split("|-d|")[1].split("|-m|")[0]
    assert len(description) == 160 and description.endswith("...")
    assert f"Cause: {cause}" in alert


def test_an_unrecognized_failure_uses_the_last_line(fakes):
    _, _, sent = run(fakes, FAKE_REFRESH_CODE="1", FAKE_REFRESH_OUT="something odd\\nlast words\\n")
    [alert] = sent
    assert "-d|last words|" in alert


def test_a_busy_job_warns(fakes):
    code, _, sent = run(fakes, FAKE_REFRESH_CODE="75", FAKE_REFRESH_OUT="another job is running")
    assert code == 1
    [warning] = sent
    assert "-i|warning|-s|Run PTC: nightly refresh skipped" in warning


def test_test_alert_mode(fakes):
    code, out, sent = run(fakes, TEST_ALERT="1")
    assert code == 0 and "=== sync" not in out
    assert sent == ["-e|Run PTC|-i|alert|-s|Run PTC: test alert|-d|Nightly job alerts will arrive like this."]
