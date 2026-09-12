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


def test_a_failed_job_alerts_and_the_other_still_runs(fakes):
    code, out, sent = run(fakes, FAKE_SYNC_CODE="1",
                          FAKE_SYNC_OUT="Traceback\\nRuntimeError: Intervals said no",
                          FAKE_REFRESH_OUT="city data unchanged")
    assert code == 1
    [alert] = sent
    assert "-i|alert|-s|Run PTC: nightly sync failed|-d|RuntimeError: Intervals said no|-m|" in alert
    assert "=== refresh" in out


def test_a_busy_job_warns(fakes):
    code, _, sent = run(fakes, FAKE_REFRESH_CODE="75", FAKE_REFRESH_OUT="another job is running")
    assert code == 1
    [warning] = sent
    assert "-i|warning|-s|Run PTC: nightly refresh skipped" in warning


def test_test_alert_mode(fakes):
    code, out, sent = run(fakes, TEST_ALERT="1")
    assert code == 0 and "=== sync" not in out
    assert sent == ["-e|Run PTC|-i|alert|-s|Run PTC: test alert|-d|Nightly job alerts will arrive like this."]
