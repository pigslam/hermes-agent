"""Minimal unattended supervisor for Reuben/Hermes scaffold benchmark jobs.

The supervisor intentionally does not invent benchmark parameters.  It only
persists an explicit queue, starts one queued command at a time in an isolated
run directory, watches heartbeats/output, and retries safe failures up to the
job's configured limit.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    import psutil
except Exception:  # pragma: no cover - psutil is a core dependency.
    psutil = None  # type: ignore[assignment]

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is a core dependency.
    yaml = None  # type: ignore[assignment]


DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_STALL_TIMEOUT_S = 300.0
DEFAULT_TIMEOUT_S = 3600.0
DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_KILL_GRACE_S = 5.0
DEFAULT_LOAD_FACTOR = 0.75
DEFAULT_USER_ACTIVE_WINDOW_S = 15 * 60.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_root() -> Path:
    raw = os.environ.get("REUBEN_BENCH_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "benchmarks" / "scaffold" / "runs" / "supervisor"


def now_ts() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), timezone.utc).isoformat()


def parse_duration(raw: str) -> float:
    text = str(raw).strip().lower()
    if not text:
        raise ValueError("duration is required")
    units = {
        "s": 1,
        "sec": 1,
        "secs": 1,
        "second": 1,
        "seconds": 1,
        "m": 60,
        "min": 60,
        "mins": 60,
        "minute": 60,
        "minutes": 60,
        "h": 3600,
        "hr": 3600,
        "hrs": 3600,
        "hour": 3600,
        "hours": 3600,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda item: -len(item[0])):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            if not number:
                raise ValueError(f"invalid duration: {raw!r}")
            return float(number) * multiplier
    return float(text)


def normalize_command(command: str | Sequence[Any]) -> list[str]:
    if isinstance(command, str):
        parts = shlex.split(command)
    else:
        parts = [str(part) for part in command]
    if not parts:
        raise ValueError("command must not be empty")
    return parts


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class RunOnceResult:
    ran: bool
    reason: str
    job_id: int | None = None
    attempt_id: int | None = None
    exit_code: int | None = None


class BenchmarkSupervisor:
    """SQLite-backed queue and subprocess supervisor."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.db_path = self.root / "queue.sqlite3"
        self.log_path = self.root / "logs" / "supervisor.log"
        self.user_active_path = self.root / "user_activity"
        self._ensure_layout()
        self._init_db()

    def _ensure_layout(self) -> None:
        for path in (self.root, self.root / "logs", self.root / "jobs"):
            path.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    env_json TEXT NOT NULL DEFAULT '{}',
                    scenario TEXT,
                    profile TEXT,
                    sweep TEXT,
                    workspace_fixture TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 100,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    timeout_s REAL NOT NULL DEFAULT 3600,
                    stall_timeout_s REAL NOT NULL DEFAULT 300,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    run_dir TEXT,
                    CHECK (status IN (
                        'queued', 'running', 'succeeded', 'failed',
                        'retryable', 'needs_review', 'snoozed'
                    ))
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    pid INTEGER,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    exit_code INTEGER,
                    heartbeat_time REAL,
                    last_output_time REAL,
                    attempt_dir TEXT NOT NULL,
                    stdout_path TEXT NOT NULL,
                    stderr_path TEXT NOT NULL,
                    events_path TEXT NOT NULL,
                    failure_reason TEXT,
                    UNIQUE(job_id, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_priority
                    ON jobs(status, priority, created_at);
                CREATE INDEX IF NOT EXISTS idx_attempts_job
                    ON attempts(job_id, attempt_no);
                """
            )

    def log(self, message: str) -> None:
        line = f"{iso()} {message}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def set_meta(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, _json_dumps(value)),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        return _json_loads(row["value"], default)

    def enqueue(
        self,
        command: str | Sequence[Any],
        *,
        name: str | None = None,
        scenario: str | None = None,
        profile: str | None = None,
        sweep: str | None = None,
        workspace_fixture: str | None = None,
        env: dict[str, str] | None = None,
        priority: int = 100,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
    ) -> int:
        command_parts = normalize_command(command)
        created = now_ts()
        job_name = name or Path(command_parts[0]).name
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs(
                    name, command_json, env_json, scenario, profile, sweep,
                    workspace_fixture, priority, max_attempts, timeout_s,
                    stall_timeout_s, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_name,
                    _json_dumps(command_parts),
                    _json_dumps(env or {}),
                    scenario,
                    profile,
                    sweep,
                    workspace_fixture,
                    priority,
                    max_attempts,
                    timeout_s,
                    stall_timeout_s,
                    created,
                    created,
                ),
            )
            job_id = int(cur.lastrowid)
            run_dir = self.root / "jobs" / f"job_{job_id:06d}"
            conn.execute(
                "UPDATE jobs SET run_dir = ?, updated_at = ? WHERE id = ?",
                (str(run_dir), now_ts(), job_id),
            )
        self.log(f"queued job={job_id} name={job_name!r} command={command_parts!r}")
        return job_id

    def enqueue_file(self, path: Path | str) -> list[int]:
        queue_path = Path(path)
        data = self._load_structured_file(queue_path)
        raw_jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(raw_jobs, list):
            raise ValueError(f"{queue_path} must contain a top-level jobs list")
        job_ids: list[int] = []
        for raw_job in raw_jobs:
            if not isinstance(raw_job, dict):
                raise ValueError("each queued job must be a mapping")
            command = raw_job.get("command") or raw_job.get("cmd")
            if command is None:
                raise ValueError("queued job is missing command")
            job_ids.append(
                self.enqueue(
                    command,
                    name=raw_job.get("name"),
                    scenario=raw_job.get("scenario"),
                    profile=raw_job.get("profile"),
                    sweep=raw_job.get("sweep") or data.get("id"),
                    workspace_fixture=raw_job.get("workspace_fixture"),
                    env=raw_job.get("env") or {},
                    priority=int(raw_job.get("priority", 100)),
                    max_attempts=int(raw_job.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
                    timeout_s=float(raw_job.get("timeout_s", DEFAULT_TIMEOUT_S)),
                    stall_timeout_s=float(
                        raw_job.get("stall_timeout_s", DEFAULT_STALL_TIMEOUT_S)
                    ),
                )
            )
        return job_ids

    @staticmethod
    def _load_structured_file(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            if yaml is None:
                raise RuntimeError("PyYAML is required to load queue YAML files")
            data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a mapping")
        return data

    def list_jobs(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        j.*,
                        (
                            SELECT COUNT(*) FROM attempts a WHERE a.job_id = j.id
                        ) AS attempts
                    FROM jobs j
                    ORDER BY
                        CASE j.status
                            WHEN 'running' THEN 0
                            WHEN 'queued' THEN 1
                            WHEN 'retryable' THEN 2
                            WHEN 'snoozed' THEN 3
                            WHEN 'needs_review' THEN 4
                            WHEN 'failed' THEN 5
                            WHEN 'succeeded' THEN 6
                            ELSE 9
                        END,
                        j.priority ASC,
                        j.created_at ASC
                    """
                )
            )

    def pause(self) -> None:
        self.set_meta("paused", True)
        self.log("manual pause enabled")

    def resume(self) -> None:
        self.set_meta("paused", False)
        self.set_meta("snooze_until", 0)
        self.log("manual pause/snooze cleared")

    def snooze(self, duration_s: float) -> float:
        until = now_ts() + duration_s
        self.set_meta("snooze_until", until)
        self.log(f"snoozed until={iso(until)} duration_s={duration_s:.1f}")
        return until

    def safety_gate(
        self,
        *,
        ignore_gates: bool = False,
        max_load_factor: float = DEFAULT_LOAD_FACTOR,
        user_active_window_s: float = DEFAULT_USER_ACTIVE_WINDOW_S,
        user_active_file: Path | None = None,
    ) -> GateResult:
        if ignore_gates:
            return GateResult(True)
        if bool(self.get_meta("paused", False)):
            return GateResult(False, "paused")
        snooze_until = float(self.get_meta("snooze_until", 0) or 0)
        if snooze_until > now_ts():
            return GateResult(False, f"snoozed until {iso(snooze_until)}")
        activity_path = user_active_file or self.user_active_path
        if user_active_window_s > 0 and activity_path.exists():
            age = now_ts() - activity_path.stat().st_mtime
            if age < user_active_window_s:
                return GateResult(
                    False,
                    f"user activity file touched {age:.0f}s ago",
                )
        if max_load_factor > 0 and hasattr(os, "getloadavg"):
            try:
                load_1m = os.getloadavg()[0]
                cpu_count = os.cpu_count() or 1
                if load_1m > cpu_count * max_load_factor:
                    return GateResult(
                        False,
                        (
                            f"load too high: {load_1m:.2f} > "
                            f"{cpu_count * max_load_factor:.2f}"
                        ),
                    )
            except OSError:
                pass
        return GateResult(True)

    def run_once(
        self,
        *,
        ignore_gates: bool = False,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        kill_grace_s: float = DEFAULT_KILL_GRACE_S,
        max_load_factor: float = DEFAULT_LOAD_FACTOR,
        user_active_window_s: float = DEFAULT_USER_ACTIVE_WINDOW_S,
    ) -> RunOnceResult:
        self.reap_stale_running(kill_grace_s=kill_grace_s)
        gate = self.safety_gate(
            ignore_gates=ignore_gates,
            max_load_factor=max_load_factor,
            user_active_window_s=user_active_window_s,
        )
        if not gate.allowed:
            self.log(f"run_once skipped: {gate.reason}")
            return RunOnceResult(False, gate.reason)
        job = self._claim_next_job()
        if job is None:
            return RunOnceResult(False, "no queued jobs")
        return self._run_attempt(
            job,
            poll_interval_s=poll_interval_s,
            kill_grace_s=kill_grace_s,
        )

    def _claim_next_job(self) -> sqlite3.Row | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'retryable')
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (now_ts(), row["id"]),
            )
            conn.commit()
        with self._connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()

    def _run_attempt(
        self,
        job: sqlite3.Row,
        *,
        poll_interval_s: float,
        kill_grace_s: float,
    ) -> RunOnceResult:
        job_id = int(job["id"])
        attempt_no = self._next_attempt_no(job_id)
        attempt_dir = Path(job["run_dir"]) / f"attempt_{attempt_no:02d}_{int(now_ts())}"
        workspace = attempt_dir / "workspace"
        hermes_home = attempt_dir / "hermes_home"
        logs_dir = attempt_dir / "logs"
        capture_dir = attempt_dir / "capture"
        for path in (workspace, hermes_home, logs_dir, capture_dir):
            path.mkdir(parents=True, exist_ok=True)

        fixture = job["workspace_fixture"]
        if fixture:
            self._copy_fixture(Path(fixture), workspace)

        stdout_path = logs_dir / "stdout.log"
        stderr_path = logs_dir / "stderr.log"
        events_path = capture_dir / "events.jsonl"
        heartbeat_path = capture_dir / "heartbeat.json"
        self._append_event(events_path, {"event": "attempt.start", "job_id": job_id})

        start = now_ts()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO attempts(
                    job_id, attempt_no, start_time, heartbeat_time,
                    last_output_time, attempt_dir, stdout_path, stderr_path,
                    events_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    attempt_no,
                    start,
                    start,
                    start,
                    str(attempt_dir),
                    str(stdout_path),
                    str(stderr_path),
                    str(events_path),
                ),
            )
            attempt_id = int(cur.lastrowid)

        command = normalize_command(_json_loads(job["command_json"], []))
        env = self._build_child_env(
            job,
            attempt_id,
            attempt_no,
            attempt_dir,
            heartbeat_path,
            events_path,
        )
        self.log(f"starting job={job_id} attempt={attempt_no} command={command!r}")

        proc: subprocess.Popen[bytes] | None = None
        failure_reason = ""
        exit_code: int | None = None
        with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open(
            "ab", buffering=0
        ) as stderr:
            try:
                popen_kwargs: dict[str, Any] = {
                    "cwd": str(workspace),
                    "env": env,
                    "stdout": stdout,
                    "stderr": stderr,
                }
                if os.name == "nt":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["start_new_session"] = True
                proc = subprocess.Popen(command, **popen_kwargs)
            except Exception as exc:
                failure_reason = f"start failed: {exc}"
                exit_code = -1
                self.log(f"job={job_id} attempt={attempt_no} {failure_reason}")

            if proc is not None:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE attempts SET pid = ? WHERE id = ?",
                        (proc.pid, attempt_id),
                    )
                exit_code, failure_reason = self._monitor_process(
                    proc,
                    job,
                    attempt_id,
                    heartbeat_path,
                    stdout_path,
                    stderr_path,
                    poll_interval_s=poll_interval_s,
                    kill_grace_s=kill_grace_s,
                )

        end = now_ts()
        self._append_event(
            events_path,
            {
                "event": "attempt.end",
                "job_id": job_id,
                "attempt_no": attempt_no,
                "exit_code": exit_code,
                "failure_reason": failure_reason,
            },
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE attempts
                SET end_time = ?, exit_code = ?, failure_reason = ?
                WHERE id = ?
                """,
                (end, exit_code, failure_reason or None, attempt_id),
            )
        self._finish_job_after_attempt(
            job_id,
            attempt_no,
            int(job["max_attempts"]),
            exit_code,
            failure_reason,
        )
        status = self.get_job(job_id)["status"]
        self.log(
            f"finished job={job_id} attempt={attempt_no} status={status} "
            f"exit_code={exit_code} reason={failure_reason!r}"
        )
        return RunOnceResult(
            True,
            status,
            job_id=job_id,
            attempt_id=attempt_id,
            exit_code=exit_code,
        )

    def _next_attempt_no(self, job_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no
                FROM attempts
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return int(row["next_no"])

    def _copy_fixture(self, fixture: Path, workspace: Path) -> None:
        source = fixture.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"workspace fixture not found: {source}")
        if source.is_dir():
            for child in source.iterdir():
                target = workspace / child.name
                if child.is_dir():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)
        else:
            shutil.copy2(source, workspace / source.name)

    def _build_child_env(
        self,
        job: sqlite3.Row,
        attempt_id: int,
        attempt_no: int,
        attempt_dir: Path,
        heartbeat_path: Path,
        events_path: Path,
    ) -> dict[str, str]:
        env = dict(os.environ)
        job_env = _json_loads(job["env_json"], {})
        env.update({str(k): str(v) for k, v in job_env.items()})
        repo_root = str(_repo_root())
        env["PYTHONPATH"] = (
            repo_root
            if not env.get("PYTHONPATH")
            else repo_root + os.pathsep + env["PYTHONPATH"]
        )
        env.update(
            {
                "HERMES_HOME": str(attempt_dir / "hermes_home"),
                "REUBEN_BENCH_RUN_DIR": str(attempt_dir),
                "REUBEN_BENCH_WORKSPACE": str(attempt_dir / "workspace"),
                "REUBEN_BENCH_CAPTURE": str(attempt_dir / "capture"),
                "REUBEN_BENCH_LOGS": str(attempt_dir / "logs"),
                "REUBEN_BENCH_HEARTBEAT": str(heartbeat_path),
                "REUBEN_BENCH_EVENTS": str(events_path),
                "REUBEN_BENCH_JOB_ID": str(job["id"]),
                "REUBEN_BENCH_ATTEMPT_ID": str(attempt_id),
                "REUBEN_BENCH_ATTEMPT_NO": str(attempt_no),
            }
        )
        return env

    def _monitor_process(
        self,
        proc: subprocess.Popen[bytes],
        job: sqlite3.Row,
        attempt_id: int,
        heartbeat_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        *,
        poll_interval_s: float,
        kill_grace_s: float,
    ) -> tuple[int, str]:
        start = now_ts()
        timeout_s = float(job["timeout_s"])
        stall_timeout_s = float(job["stall_timeout_s"])
        last_db_update = 0.0
        failure_reason = ""

        while True:
            exit_code = proc.poll()
            heartbeat_time = self._mtime_or_none(heartbeat_path)
            output_time = max(
                self._mtime_or_none(stdout_path) or start,
                self._mtime_or_none(stderr_path) or start,
            )
            activity_time = max(start, heartbeat_time or start, output_time)
            current = now_ts()
            if current - last_db_update >= 1.0:
                with self._connect() as conn:
                    conn.execute(
                        """
                        UPDATE attempts
                        SET heartbeat_time = ?, last_output_time = ?
                        WHERE id = ?
                        """,
                        (heartbeat_time or start, output_time, attempt_id),
                    )
                last_db_update = current

            if exit_code is not None:
                if exit_code != 0 and not failure_reason:
                    failure_reason = f"exit code {exit_code}"
                return int(exit_code), failure_reason

            if timeout_s > 0 and current - start > timeout_s:
                failure_reason = f"timeout after {timeout_s:.0f}s"
                exit_code = self._stop_process(proc.pid, kill_grace_s=kill_grace_s)
                try:
                    proc.wait(timeout=max(0.1, kill_grace_s))
                    exit_code = int(proc.returncode)
                except subprocess.TimeoutExpired:
                    pass
                return exit_code, failure_reason

            if stall_timeout_s > 0 and current - activity_time > stall_timeout_s:
                failure_reason = (
                    f"stalled after {stall_timeout_s:.0f}s "
                    "without heartbeat/output"
                )
                exit_code = self._stop_process(proc.pid, kill_grace_s=kill_grace_s)
                try:
                    proc.wait(timeout=max(0.1, kill_grace_s))
                    exit_code = int(proc.returncode)
                except subprocess.TimeoutExpired:
                    pass
                return exit_code, failure_reason

            time.sleep(max(0.05, poll_interval_s))

    @staticmethod
    def _mtime_or_none(path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return None

    def _stop_process(self, pid: int, *, kill_grace_s: float) -> int:
        self.log(f"terminating pid={pid}")
        self._signal_process_tree(pid, kill=False)
        deadline = now_ts() + kill_grace_s
        while now_ts() < deadline:
            if not self._pid_exists(pid):
                return -signal.SIGTERM
            time.sleep(0.1)
        self.log(f"killing pid={pid}")
        self._signal_process_tree(pid, kill=True)
        return -signal.SIGKILL

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if psutil is not None:
            return psutil.pid_exists(pid)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _signal_process_tree(pid: int, *, kill: bool) -> None:
        if psutil is not None:
            try:
                parent = psutil.Process(pid)
                children = parent.children(recursive=True)
                targets = children + [parent]
                for proc in targets:
                    try:
                        proc.kill() if kill else proc.terminate()
                    except psutil.NoSuchProcess:
                        pass
                return
            except psutil.NoSuchProcess:
                return
        if os.name != "nt":
            try:
                os.killpg(pid, signal.SIGKILL if kill else signal.SIGTERM)
                return
            except OSError:
                pass
        try:
            os.kill(pid, signal.SIGKILL if kill else signal.SIGTERM)
        except OSError:
            pass

    def _finish_job_after_attempt(
        self,
        job_id: int,
        attempt_no: int,
        max_attempts: int,
        exit_code: int | None,
        failure_reason: str,
    ) -> None:
        if exit_code == 0:
            status = "succeeded"
        elif attempt_no < max_attempts:
            status = "retryable"
        else:
            status = "needs_review"
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_ts(), job_id),
            )

    def reap_stale_running(
        self,
        *,
        kill_grace_s: float = DEFAULT_KILL_GRACE_S,
    ) -> None:
        with self._connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT j.*, a.id AS attempt_id, a.pid, a.attempt_no,
                           a.heartbeat_time, a.last_output_time
                    FROM jobs j
                    JOIN attempts a ON a.job_id = j.id
                    WHERE j.status = 'running'
                      AND a.attempt_no = (
                          SELECT MAX(attempt_no) FROM attempts WHERE job_id = j.id
                      )
                    """
                )
            )
        for row in rows:
            pid = row["pid"]
            alive = bool(pid and self._pid_exists(int(pid)))
            activity = max(
                float(row["heartbeat_time"] or 0),
                float(row["last_output_time"] or 0),
                float(row["updated_at"] or 0),
            )
            stale = now_ts() - activity > float(row["stall_timeout_s"])
            if alive and not stale:
                continue
            reason = "stale running job reaped"
            exit_code = -1
            if alive and stale and pid:
                reason = "stale running job killed"
                exit_code = self._stop_process(int(pid), kill_grace_s=kill_grace_s)
            elif not alive:
                reason = "running pid is no longer alive"
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE attempts
                    SET end_time = ?, exit_code = ?, failure_reason = ?
                    WHERE id = ? AND end_time IS NULL
                    """,
                    (now_ts(), exit_code, reason, row["attempt_id"]),
                )
            self._finish_job_after_attempt(
                int(row["id"]),
                int(row["attempt_no"]),
                int(row["max_attempts"]),
                exit_code,
                reason,
            )
            self.log(
                f"reaped job={row['id']} attempt={row['attempt_no']} "
                f"reason={reason}"
            )

    def get_job(self, job_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        return row

    def list_attempts(self, job_id: int | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM attempts"
        params: tuple[Any, ...] = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            params = (job_id,)
        query += " ORDER BY start_time ASC"
        with self._connect() as conn:
            return list(conn.execute(query, params))

    @staticmethod
    def _append_event(path: Path, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload.setdefault("time", iso())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_json_dumps(payload) + "\n")


def write_heartbeat(message: str | None = None, **extra: Any) -> None:
    """Helper for benchmark jobs that want to report liveness."""

    path = os.environ.get("REUBEN_BENCH_HEARTBEAT", "").strip()
    if not path:
        return
    payload = {"time": iso(), "pid": os.getpid()}
    if message:
        payload["message"] = message
    payload.update(extra)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(_json_dumps(payload), encoding="utf-8")


def write_event(event: str, **payload: Any) -> None:
    """Helper for benchmark jobs that want to append structured events."""

    path = os.environ.get("REUBEN_BENCH_EVENTS", "").strip()
    if not path:
        return
    data = {"event": event, "time": iso(), **payload}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_json_dumps(data) + "\n")


def _print_status(supervisor: BenchmarkSupervisor, *, json_output: bool = False) -> None:
    rows = supervisor.list_jobs()
    if json_output:
        print(
            json.dumps(
                [
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "status": row["status"],
                        "attempts": row["attempts"],
                        "max_attempts": row["max_attempts"],
                        "scenario": row["scenario"],
                        "profile": row["profile"],
                        "run_dir": row["run_dir"],
                    }
                    for row in rows
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return
    if not rows:
        print("No benchmark jobs queued.")
        return
    print(
        f"{'id':>4}  {'status':<13}  {'attempts':<8}  "
        f"{'name':<24}  scenario/profile"
    )
    for row in rows:
        scenario_profile = "/".join(
            part for part in (row["scenario"], row["profile"]) if part
        )
        print(
            f"{row['id']:>4}  {row['status']:<13}  "
            f"{row['attempts']}/{row['max_attempts']:<6}  "
            f"{row['name'][:24]:<24}  {scenario_profile}"
        )


def _tail_file(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return [f"{path} does not exist\n"]
    buf: deque[str] = deque(maxlen=lines)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            buf.append(line)
    return list(buf)


def _print_tail(supervisor: BenchmarkSupervisor, *, job_id: int | None, lines: int) -> None:
    print(f"== {supervisor.log_path} ==")
    print("".join(_tail_file(supervisor.log_path, lines)), end="")
    if job_id is None:
        return
    attempts = supervisor.list_attempts(job_id)
    if not attempts:
        print(f"\nNo attempts for job {job_id}.")
        return
    latest = attempts[-1]
    for label, key in (("stdout", "stdout_path"), ("stderr", "stderr_path")):
        path = Path(latest[key])
        print(f"\n== job {job_id} attempt {latest['attempt_no']} {label}: {path} ==")
        print("".join(_tail_file(path, lines)), end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reuben-bench")
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root(),
        help="Supervisor state root. Default: benchmarks/scaffold/runs/supervisor",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    queue = sub.add_parser("queue", help="Enqueue predefined benchmark jobs")
    queue.add_argument("--cmd", help="Explicit command to enqueue")
    queue.add_argument(
        "--file",
        type=Path,
        help="YAML/JSON file with a top-level jobs list",
    )
    queue.add_argument("--name")
    queue.add_argument("--scenario")
    queue.add_argument("--profile")
    queue.add_argument("--sweep")
    queue.add_argument("--workspace-fixture")
    queue.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    queue.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    queue.add_argument("--stall-timeout-s", type=float, default=DEFAULT_STALL_TIMEOUT_S)
    queue.add_argument("--priority", type=int, default=100)

    status = sub.add_parser("status", help="Show queue status")
    status.add_argument("--json", action="store_true")

    run_once = sub.add_parser("run-once", help="Run one queued attempt")
    run_once.add_argument("--ignore-gates", action="store_true")
    run_once.add_argument("--poll-interval-s", type=float, default=DEFAULT_POLL_INTERVAL_S)
    run_once.add_argument("--kill-grace-s", type=float, default=DEFAULT_KILL_GRACE_S)
    run_once.add_argument("--max-load-factor", type=float, default=DEFAULT_LOAD_FACTOR)
    run_once.add_argument(
        "--user-active-window-s",
        type=float,
        default=DEFAULT_USER_ACTIVE_WINDOW_S,
    )

    daemon = sub.add_parser("daemon", help="Keep running queued jobs")
    daemon.add_argument("--ignore-gates", action="store_true")
    daemon.add_argument("--sleep-s", type=float, default=5.0)
    daemon.add_argument("--poll-interval-s", type=float, default=DEFAULT_POLL_INTERVAL_S)
    daemon.add_argument("--kill-grace-s", type=float, default=DEFAULT_KILL_GRACE_S)
    daemon.add_argument("--max-load-factor", type=float, default=DEFAULT_LOAD_FACTOR)
    daemon.add_argument(
        "--user-active-window-s",
        type=float,
        default=DEFAULT_USER_ACTIVE_WINDOW_S,
    )
    daemon.add_argument(
        "--max-loops",
        type=int,
        default=0,
        help="Testing hook; 0 means forever",
    )

    sub.add_parser("pause", help="Pause starting new jobs")
    sub.add_parser("resume", help="Clear manual pause and snooze")
    snooze = sub.add_parser("snooze", help="Pause new jobs for a duration like 30m or 2h")
    snooze.add_argument("duration")

    tail = sub.add_parser("tail", help="Show supervisor and latest attempt logs")
    tail.add_argument("--job-id", type=int)
    tail.add_argument("--lines", type=int, default=80)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    supervisor = BenchmarkSupervisor(args.root)

    if args.command == "queue":
        ids: list[int] = []
        if args.file:
            ids.extend(supervisor.enqueue_file(args.file))
        if args.cmd:
            ids.append(
                supervisor.enqueue(
                    args.cmd,
                    name=args.name,
                    scenario=args.scenario,
                    profile=args.profile,
                    sweep=args.sweep,
                    workspace_fixture=args.workspace_fixture,
                    max_attempts=args.max_attempts,
                    timeout_s=args.timeout_s,
                    stall_timeout_s=args.stall_timeout_s,
                    priority=args.priority,
                )
            )
        if not ids:
            parser.error("queue requires --cmd or --file")
        print("queued " + ", ".join(str(job_id) for job_id in ids))
        return 0

    if args.command == "status":
        _print_status(supervisor, json_output=args.json)
        return 0

    if args.command == "run-once":
        result = supervisor.run_once(
            ignore_gates=args.ignore_gates,
            poll_interval_s=args.poll_interval_s,
            kill_grace_s=args.kill_grace_s,
            max_load_factor=args.max_load_factor,
            user_active_window_s=args.user_active_window_s,
        )
        print(result.reason)
        return 0 if result.ran or result.reason == "no queued jobs" else 2

    if args.command == "daemon":
        loops = 0
        supervisor.log("daemon started")
        try:
            while True:
                result = supervisor.run_once(
                    ignore_gates=args.ignore_gates,
                    poll_interval_s=args.poll_interval_s,
                    kill_grace_s=args.kill_grace_s,
                    max_load_factor=args.max_load_factor,
                    user_active_window_s=args.user_active_window_s,
                )
                if result.ran:
                    continue
                loops += 1
                if args.max_loops and loops >= args.max_loops:
                    break
                time.sleep(max(0.1, args.sleep_s))
        except KeyboardInterrupt:
            supervisor.log("daemon interrupted")
            return 130
        supervisor.log("daemon stopped")
        return 0

    if args.command == "pause":
        supervisor.pause()
        print("paused")
        return 0

    if args.command == "resume":
        supervisor.resume()
        print("resumed")
        return 0

    if args.command == "snooze":
        until = supervisor.snooze(parse_duration(args.duration))
        print(f"snoozed until {iso(until)}")
        return 0

    if args.command == "tail":
        _print_tail(supervisor, job_id=args.job_id, lines=args.lines)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
