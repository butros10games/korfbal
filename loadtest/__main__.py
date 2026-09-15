"""Run bounded experiments on owned local services and always remove those services."""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
from time import monotonic, sleep
from typing import Any, TextIO
from uuid import uuid4

import aiohttp
import psycopg

from .workload import Workload


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[2]
POSTGRES_PORT = 5432
MAX_WORKLOAD_SIZE = 10_000
WRITE_INTERVAL_BOUNDS = (0.1, 300)
MAX_LATENCY_BUDGET = 60_000
HTTP_OK = 200
DOCKER = shutil.which("docker") or "/usr/bin/docker"


def command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stderr: TextIO | None = None,
) -> str:
    """Run an argument vector without shell interpolation."""
    return subprocess.check_output(
        args, text=True, cwd=cwd, env=env, stderr=stderr
    ).strip()


def stop_process(process: subprocess.Popen[Any]) -> None:
    """Terminate only an owned process group, including its worker children."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def container(
    stack: ExitStack, image: str, port: int, args: list[str]
) -> tuple[str, str]:
    """Publish a fresh container on a random loopback port; retain no volumes."""
    name = f"korfbal-loadtest-{uuid4().hex[:12]}"
    container_id = command([
        DOCKER,
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        "korfbal.synthetic-loadtest=true",
        "--memory",
        "1g",
        "--cpus",
        "2",
        "--publish",
        f"127.0.0.1::{port}",
        *args,
        image,
        *(
            ["-c", "shared_preload_libraries=pg_stat_statements"]
            if port == POSTGRES_PORT
            else []
        ),
    ])
    stack.callback(
        subprocess.run,
        [DOCKER, "rm", "--force", "--volumes", container_id],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    published = command([DOCKER, "port", container_id, str(port)]).rsplit(":", 1)[1]
    return container_id, published


async def database_snapshot(port: str) -> dict[str, Any]:
    """Read counters only; never store SQL text, session IDs or row payloads."""
    async with (
        await psycopg.AsyncConnection.connect(
            host="127.0.0.1",
            port=port,
            dbname="korfbal_loadtest",
            user="postgres",
            autocommit=True,
        ) as connection,
        connection.cursor() as cursor,
    ):
        await cursor.execute(
            "SELECT coalesce(sum(calls),0), coalesce(sum(total_exec_time),0) "
            "FROM pg_stat_statements"
        )
        calls, sql_ms = await cursor.fetchone() or (0, 0)
        await cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE wait_event_type = 'Lock') "
            "FROM pg_stat_activity WHERE datname = current_database()"
        )
        connections, lock_waiters = await cursor.fetchone() or (0, 0)
        await cursor.execute(
            "SELECT count(*) FILTER (WHERE generation > completed_generation), "
            "count(*) FILTER (WHERE error <> ''), "
            "coalesce(sum(completed_generation),0) FROM kwt_common_backgroundjob"
        )
        pending, errors, completed = await cursor.fetchone() or (0, 0, 0)
        return {
            "sql_calls": int(calls),
            "sql_ms": float(sql_ms),
            "connections": connections,
            "lock_waiters": lock_waiters,
            "pending_jobs": pending,
            "job_errors": errors,
            "completed_job_generations": int(completed),
        }


async def warmup(
    origin: str, fixture: dict[str, Any], *, db_port: str, background_jobs: bool
) -> None:
    """Check startup and prime public routes before collecting timings.

    Raises:
        RuntimeError: Startup or a warmup request failed.

    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as client:
        for _ in range(60):
            try:
                async with client.get(
                    origin + f"/api/matches/{fixture['match_id']}/live/"
                ) as response:
                    if response.status == HTTP_OK:
                        break
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("ASGI did not become ready; inspect server.log.")
    # Let fixture-generated projection work settle before measuring traffic.
    for _ in range(60 if background_jobs else 1):
        if (await database_snapshot(db_port))["pending_jobs"] == 0:
            break
        await asyncio.sleep(0.5)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(force_close=True),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as client:

        async def warm(resource: str) -> None:
            async with client.get(
                origin + f"/api/matches/{fixture['match_id']}/{resource}/"
            ) as response:
                await response.read()
                if response.status != HTTP_OK:
                    raise RuntimeError(f"Warmup {resource} returned {response.status}.")

        await asyncio.gather(
            *(
                warm(resource)
                for resource in ("live", "summary", "events", "shots", "stats")
                for _ in range(4)
            )
        )


async def experiment(
    origin: str,
    fixtures: list[dict[str, Any]],
    options: argparse.Namespace,
    db_port: str,
) -> dict[str, Any]:
    """Wait for ASGI readiness, then execute each configured viewer level."""
    await warmup(
        origin, fixtures[0], db_port=db_port, background_jobs=options.background_jobs
    )
    reports = []
    for viewers in options.viewers:
        print(f"Measuring {viewers} viewers / {len(fixtures)} matches...", flush=True)
        baseline = await database_snapshot(db_port)
        snapshots = []
        monitoring_errors: list[str] = []

        async def monitor(samples: list[dict[str, Any]], errors: list[str]) -> None:
            while True:
                try:
                    samples.append(await database_snapshot(db_port))
                except psycopg.Error as error:
                    errors.append(type(error).__name__)
                await asyncio.sleep(0.5)

        monitor_task = asyncio.create_task(monitor(snapshots, monitoring_errors))
        try:
            report = await Workload(
                origin,
                fixtures,
                viewers=viewers,
                seconds=options.seconds,
                interval=options.write_interval,
                reconnect=options.reconnect,
            ).run()
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        after = await database_snapshot(db_port)
        report["database"] = {
            "monitoring_errors": monitoring_errors,
            "sql_calls": after["sql_calls"] - baseline["sql_calls"],
            "sql_execution_ms": round(after["sql_ms"] - baseline["sql_ms"], 2),
            "peak_connections_sampled": max(
                (row["connections"] for row in snapshots), default=0
            ),
            "peak_lock_waiters_sampled": max(
                (row["lock_waiters"] for row in snapshots), default=0
            ),
            "pending_jobs_before": baseline["pending_jobs"],
            "pending_jobs_after": after["pending_jobs"],
            "job_errors_after": after["job_errors"],
            "completed_job_generations": after["completed_job_generations"]
            - baseline["completed_job_generations"],
        }
        report["passed"] = passes(report, options.max_p95_ms)
        reports.append(report)
        print(json.dumps(report), flush=True)
    return {"phases": reports, "passed": all(report["passed"] for report in reports)}


def passes(report: dict[str, Any], max_p95_ms: float) -> bool:
    """Reject missing samples, transport errors, stale views and missed writes."""
    counters = report["counters"]
    command_latency = report["latency"].get("tracker_command", {})
    return bool(
        command_latency.get("count", 0) > 0
        and command_latency.get("p95_ms", float("inf")) <= max_p95_ms
        and not any(
            counters.get(key, 0)
            for key in (
                "http_errors",
                "invalid_json",
                "sse_errors",
                "stale_viewers",
                "stale_live_reads",
                "http_requests_cancelled",
                "missed_write_slots",
            )
        )
        and report["database"]["job_errors_after"] == 0
        and not report["database"]["monitoring_errors"]
    )


def positive_int(value: str) -> int:
    """Reject unbounded or empty workloads at argument parsing.

    Raises:
        argparse.ArgumentTypeError: The count is out of bounds.

    """
    parsed = int(value)
    if not 1 <= parsed <= MAX_WORKLOAD_SIZE:
        raise argparse.ArgumentTypeError("Expected an integer between 1 and 10000.")
    return parsed


def arguments() -> argparse.Namespace:
    """Parse bounded workload settings; deliberately provide no remote URL option."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewers", nargs="+", default=["10", "50", "100"])
    parser.add_argument("--matches", type=positive_int, default=1)
    parser.add_argument("--shots", type=positive_int, default=100)
    parser.add_argument("--seconds", type=positive_int, default=30)
    parser.add_argument("--workers", type=positive_int, default=4)
    parser.add_argument(
        "--db-pool-size",
        type=int,
        default=0,
        help="Connections per ASGI worker; 0 reproduces unpooled behavior.",
    )
    parser.add_argument("--write-interval", type=float, default=3)
    parser.add_argument("--max-p95-ms", type=float, default=1000)
    parser.add_argument("--reconnect", action="store_true")
    parser.add_argument(
        "--profile-commands",
        action="store_true",
        help="Profile 20 commands on the first seeded match instead of HTTP load.",
    )
    parser.add_argument(
        "--background-jobs", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "reports"
        / "korfbal-loadtest"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%S"),
    )
    options = parser.parse_args()
    try:
        options.viewers = [
            positive_int(part) for raw in options.viewers for part in raw.split(",")
        ]
    except (ValueError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    if not 0 <= options.db_pool_size <= MAX_WORKLOAD_SIZE:
        parser.error("DB pool size must be between 0 and 10000.")
    if (
        not WRITE_INTERVAL_BOUNDS[0]
        <= options.write_interval
        <= WRITE_INTERVAL_BOUNDS[1]
        or not 1 <= options.max_p95_ms <= MAX_LATENCY_BUDGET
    ):
        parser.error(
            "Write interval must be 0.1-300 seconds and p95 budget 1-60000 ms."
        )
    return options


def main() -> int:
    """Provision, seed, measure and clean up a synthetic-only experiment.

    Raises:
        RuntimeError: PostgreSQL did not become ready.

    """
    options = arguments()
    options.output.mkdir(parents=True, exist_ok=False)
    metadata = {
        "revision": command(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "dirty": bool(command(["git", "status", "--porcelain"], cwd=ROOT)),
        "harness_sha256": hashlib.sha256(
            b"".join(
                path.read_bytes() for path in sorted(Path(__file__).parent.glob("*.py"))
            )
        ).hexdigest(),
        "application_diff_sha256": hashlib.sha256(
            command(
                ["git", "diff", "HEAD", "--", "apps/django_projects/korfbal"], cwd=ROOT
            ).encode()
        ).hexdigest(),
        "python": sys.version.split()[0],
        "django": version("django"),
        "granian": version("granian"),
        "host_logical_cpus": os.cpu_count(),
        "asgi_workers": options.workers,
        "db_pool_size_per_web_worker": options.db_pool_size,
        "background_jobs": options.background_jobs,
        "initial_shots_per_match": options.shots,
        "max_command_p95_ms": options.max_p95_ms,
        "limitations": (
            "Local synthetic traffic; host-run API/worker/generator share CPU. "
            "No TLS, proxy, WAN, media or provider traffic. "
            "Each data container: 2 CPUs/1 GiB; PostgreSQL default 100 connections. "
            "Later phases have longer timelines and warmer caches."
        ),
    }
    print(f"Logs and results: {options.output}", flush=True)
    with ExitStack() as stack:
        postgres, db_port = container(
            stack,
            "postgres:17.6-alpine",
            5432,
            [
                "-e",
                "POSTGRES_DB=korfbal_loadtest",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
            ],
        )
        valkey, valkey_port = container(stack, "valkey/valkey:8.1.3-alpine", 6379, [])
        metadata["data_images"] = [
            command([DOCKER, "inspect", "--format", "{{.Image}}", name])
            for name in (postgres, valkey)
        ]
        deadline = monotonic() + 60
        while subprocess.run(
            [DOCKER, "exec", postgres, "pg_isready", "-U", "postgres"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode:
            if monotonic() > deadline:
                raise RuntimeError("Disposable PostgreSQL did not become ready.")
            sleep(0.5)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        secret = secrets.token_urlsafe(48)
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG")
            if key in os.environ
        }
        environment.update({
            "PYTHONUNBUFFERED": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "DJANGO_SETTINGS_MODULE": "loadtest.settings",
            "DJANGO_ENV": "production",
            "SECRET_KEY": secret,
            "KORFBAL_AUDIT_INGEST_TOKEN": secret,
            "BG_AUTH_JWT_SIGNING_KEY": secret,
            "KORFBAL_LOADTEST": "isolated",
            "KORFBAL_LOADTEST_SECRET": secret,
            "KORFBAL_LOADTEST_DB_PORT": db_port,
            "KORFBAL_LOADTEST_VALKEY_PORT": valkey_port,
            "KORFBAL_LOADTEST_ORIGIN": origin,
        })
        with (options.output / "setup.log").open("w") as log:
            subprocess.run(
                [sys.executable, "manage.py", "migrate", "--noinput"],
                cwd=PROJECT,
                env=environment,
                stdout=log,
                stderr=log,
                check=True,
            )
            raw = command(
                [
                    sys.executable,
                    "-c",
                    "import django, json, sys; django.setup(); "
                    "from loadtest.seed import seed; "
                    "print(json.dumps(seed(int(sys.argv[1]), int(sys.argv[2]))))",
                    str(options.matches),
                    str(options.shots),
                ],
                cwd=PROJECT,
                env=environment,
                stderr=log,
            )
            fixtures = json.loads(raw.splitlines()[-1])

        if options.profile_commands:
            with (options.output / "profile.log").open("w") as log:
                profiled = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import django; django.setup(); "
                        "from loadtest.profile_commands import main; main()",
                    ],
                    input=json.dumps(fixtures[0]),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    cwd=PROJECT,
                    env=environment,
                    check=True,
                )
            result = json.loads(profiled.stdout.splitlines()[-1])
            result["environment"] = metadata
            (options.output / "command-profile.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            return 0

        def start_process(name: str, args: list[str]) -> subprocess.Popen[Any]:
            log = stack.enter_context((options.output / f"{name}.log").open("w"))
            process_env = dict(environment)
            if name == "server":
                process_env["KORFBAL_DB_POOL_MAX_SIZE"] = str(options.db_pool_size)
            process = subprocess.Popen(
                args,
                cwd=PROJECT,
                env=process_env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            stack.callback(stop_process, process)
            return process

        start_process(
            "server",
            [
                sys.executable,
                "-m",
                "granian",
                "--interface",
                "asgi",
                "--no-ws",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                str(options.workers),
                "korfbal.asgi:application",
            ],
        )
        if options.background_jobs:
            start_process(
                "worker",
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "korfbal",
                    "worker",
                    "--queues",
                    "projections",
                    "--concurrency",
                    "2",
                    "--loglevel",
                    "WARNING",
                    "--hostname",
                    "loadtest@%h",
                ],
            )
        result = asyncio.run(experiment(origin, fixtures, options, db_port))
        result["environment"] = metadata
        (options.output / "results.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
