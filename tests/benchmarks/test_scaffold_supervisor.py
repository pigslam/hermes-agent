import sys
from pathlib import Path

from benchmarks.scaffold.supervisor import BenchmarkSupervisor


def _write_fake_job(tmp_path: Path) -> Path:
    script = tmp_path / "fake_job.py"
    script.write_text(
        """
import json
import os
import sys
import time
from pathlib import Path


def heartbeat(message):
    path = os.environ.get("REUBEN_BENCH_HEARTBEAT")
    if path:
        payload = {"message": message, "time": time.time()}
        Path(path).write_text(json.dumps(payload), encoding="utf-8")


mode = sys.argv[1]
heartbeat(mode)
print(f"mode={mode}", flush=True)
if mode == "success":
    sys.exit(0)
if mode == "fail":
    sys.exit(7)
if mode == "stall":
    time.sleep(60)
    sys.exit(0)
raise SystemExit(f"unknown mode: {mode}")
""".lstrip(),
        encoding="utf-8",
    )
    return script


def _run_once(supervisor: BenchmarkSupervisor):
    return supervisor.run_once(
        ignore_gates=True,
        poll_interval_s=0.05,
        kill_grace_s=0.05,
    )


def test_fake_job_succeeds(tmp_path):
    script = _write_fake_job(tmp_path)
    supervisor = BenchmarkSupervisor(tmp_path / "bench")
    job_id = supervisor.enqueue(
        [sys.executable, str(script), "success"],
        max_attempts=1,
        stall_timeout_s=5,
    )

    result = _run_once(supervisor)

    assert result.ran is True
    assert supervisor.get_job(job_id)["status"] == "succeeded"
    attempts = supervisor.list_attempts(job_id)
    assert len(attempts) == 1
    assert attempts[0]["exit_code"] == 0
    stdout = Path(attempts[0]["stdout_path"]).read_text(encoding="utf-8")
    assert "mode=success" in stdout


def test_stalled_job_is_killed_and_retried_until_review(tmp_path):
    script = _write_fake_job(tmp_path)
    supervisor = BenchmarkSupervisor(tmp_path / "bench")
    job_id = supervisor.enqueue(
        [sys.executable, str(script), "stall"],
        max_attempts=2,
        timeout_s=5,
        stall_timeout_s=0.2,
    )

    first = _run_once(supervisor)
    second = _run_once(supervisor)

    assert first.ran is True
    assert second.ran is True
    assert supervisor.get_job(job_id)["status"] == "needs_review"
    attempts = supervisor.list_attempts(job_id)
    assert len(attempts) == 2
    assert all("stalled" in (attempt["failure_reason"] or "") for attempt in attempts)
    assert all(attempt["exit_code"] != 0 for attempt in attempts)


def test_repeated_failure_becomes_needs_review(tmp_path):
    script = _write_fake_job(tmp_path)
    supervisor = BenchmarkSupervisor(tmp_path / "bench")
    job_id = supervisor.enqueue(
        [sys.executable, str(script), "fail"],
        max_attempts=2,
        stall_timeout_s=5,
    )

    first = _run_once(supervisor)
    second = _run_once(supervisor)

    assert first.ran is True
    assert second.ran is True
    assert supervisor.get_job(job_id)["status"] == "needs_review"
    attempts = supervisor.list_attempts(job_id)
    assert [attempt["exit_code"] for attempt in attempts] == [7, 7]


def test_pause_and_snooze_block_new_attempts(tmp_path):
    script = _write_fake_job(tmp_path)
    supervisor = BenchmarkSupervisor(tmp_path / "bench")
    job_id = supervisor.enqueue(
        [sys.executable, str(script), "success"],
        max_attempts=1,
        stall_timeout_s=5,
    )

    supervisor.pause()
    paused = supervisor.run_once(
        max_load_factor=0,
        user_active_window_s=0,
        poll_interval_s=0.05,
    )
    assert paused.ran is False
    assert paused.reason == "paused"
    assert supervisor.get_job(job_id)["status"] == "queued"

    supervisor.resume()
    supervisor.snooze(60)
    snoozed = supervisor.run_once(
        max_load_factor=0,
        user_active_window_s=0,
        poll_interval_s=0.05,
    )
    assert snoozed.ran is False
    assert snoozed.reason.startswith("snoozed until")
    assert supervisor.get_job(job_id)["status"] == "queued"

    supervisor.resume()
    finished = _run_once(supervisor)
    assert finished.ran is True
    assert supervisor.get_job(job_id)["status"] == "succeeded"
