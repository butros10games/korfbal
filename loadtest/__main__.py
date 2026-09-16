"""Run bounded experiments on owned local services and always remove those services."""

import argparse
import asyncio
from collections.abc import Callable
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

from .proxy import add_proxy_arguments, caddy_config, validate_cpu_partitions
from .workload import Workload


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[2]
POSTGRES_PORT = 5432
MAX_GENERATOR_SHARDS = 32
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
    stack: ExitStack, image: str, port: int, args: list[str], cpus: str | None = None
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
        *(["--cpuset-cpus", cpus] if cpus else []),
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
    read_origins: tuple[str | None, str | None, str | None] = (None, None, None),
) -> dict[str, Any]:
    """Wait for ASGI readiness, then execute each configured viewer level."""
    live_origin, sse_origin, public_origin = read_origins
    await warmup(
        origin, fixtures[0], db_port=db_port, background_jobs=options.background_jobs
    )
    if live_origin:
        await warmup(live_origin, fixtures[0], db_port=db_port, background_jobs=False)
    if sse_origin:
        await warmup(sse_origin, fixtures[0], db_port=db_port, background_jobs=False)
    if public_origin:
        await warmup(public_origin, fixtures[0], db_port=db_port, background_jobs=False)
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
                live_origin=live_origin,
                sse_origin=sse_origin,
                public_origin=public_origin,
                generator_shards=options.generator_shards,
                compact=options.compact,
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
        report["passed"] = passes(
            report, options.max_p95_ms, options.max_live_p95_ms, options.max_push_p95_ms
        )
        reports.append(report)
        print(json.dumps(report), flush=True)
    return {"phases": reports, "passed": all(report["passed"] for report in reports)}


def passes(
    report: dict[str, Any],
    max_p95_ms: float,
    max_live_p95_ms: float | None = None,
    max_push_p95_ms: float | None = None,
) -> bool:
    """Reject missing samples, transport errors, stale views and missed writes."""
    counters = report["counters"]
    if max_live_p95_ms is not None:
        live = report["latency"].get("live", {})
        if not live.get("count") or live.get("p95_ms", float("inf")) > max_live_p95_ms:
            return False
    if max_push_p95_ms is not None:
        pushed = report["latency"].get("snapshot_publish_to_receive", {})
        if (
            not pushed.get("count")
            or pushed.get("p95_ms", float("inf")) > max_push_p95_ms
        ):
            return False
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
    add_proxy_arguments(parser)
    parser.add_argument("--viewers", nargs="+", default=["10", "50", "100"])
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Use subscribed compact SSE snapshots and patches.",
    )
    parser.add_argument("--generator-shards", type=positive_int, default=1)
    parser.add_argument("--matches", type=positive_int, default=1)
    parser.add_argument("--shots", type=positive_int, default=100)
    parser.add_argument("--seconds", type=positive_int, default=30)
    parser.add_argument("--workers", type=positive_int, default=4)
    parser.add_argument("--sse-workers", type=int, default=0)
    parser.add_argument("--public-workers", type=int, default=0)
    parser.add_argument("--public-backpressure", type=positive_int, default=16384)
    parser.add_argument("--sse-backpressure", type=positive_int, default=8192)
    parser.add_argument(
        "--live-workers",
        type=int,
        default=0,
        help="Optional separate live-read process pool; 0 shares the API workers.",
    )
    parser.add_argument("--live-threads", type=positive_int, default=2)
    parser.add_argument("--live-backpressure", type=positive_int, default=16384)
    parser.add_argument("--max-live-p95-ms", type=float, default=100)
    parser.add_argument("--max-push-p95-ms", type=float, default=100)
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
        "--profile-payloads",
        action="store_true",
        help="Measure synthetic starting/goal/shot payload sizes instead of HTTP load.",
    )
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
    validate_cpu_partitions(parser, options)
    try:
        options.viewers = [
            positive_int(part) for raw in options.viewers for part in raw.split(",")
        ]
    except (ValueError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    if options.generator_shards > MAX_GENERATOR_SHARDS:
        parser.error("At most 32 local spectator generator processes are supported.")
    if not 0 <= options.public_workers <= MAX_WORKLOAD_SIZE:
        parser.error("Public worker count is outside the supported range.")
    if not 0 <= options.sse_workers <= MAX_WORKLOAD_SIZE:
        parser.error("SSE worker count is outside the supported range.")
    if not 0 <= options.live_workers <= MAX_WORKLOAD_SIZE:
        parser.error("Live worker count must be between 0 and 10000.")
    if not 1 <= options.max_push_p95_ms <= MAX_LATENCY_BUDGET:
        parser.error("Push p95 budget must be between 1 and 60000 ms.")
    if not 1 <= options.max_live_p95_ms <= MAX_LATENCY_BUDGET:
        parser.error("Live p95 budget must be between 1 and 60000 ms.")
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


def write_command_profile(
    output: Path,
    fixture: dict[str, Any],
    environment: dict[str, str],
    metadata: dict[str, Any],
    *,
    payloads: bool = False,
) -> None:
    """Profile the seeded match using the isolated experiment environment."""
    module = "profile_payloads" if payloads else "profile_commands"
    with (output / "profile.log").open("w") as log:
        profiled = subprocess.run(
            [
                sys.executable,
                "-c",
                "import django; django.setup(); "
                f"from loadtest.{module} import main; main()",
            ],
            input=json.dumps(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=log,
            cwd=PROJECT,
            env=environment,
            check=True,
        )
    result = json.loads(profiled.stdout.splitlines()[-1])
    result["environment"] = metadata
    filename = "payload-profile.json" if payloads else "command-profile.json"
    (output / filename).write_text(json.dumps(result, indent=2) + "\n")


def start_read_pool(
    start_process: Callable[[str, list[str]], subprocess.Popen[Any]],
    *,
    name: str,
    workers: int,
    threads: int | None = None,
    backpressure: int | None = None,
) -> str | None:
    """Start optional isolated read capacity on a task-owned local port."""
    if not workers:
        return None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    interface = "wsgi" if threads is not None else "asgi"
    args = [
        sys.executable,
        "-m",
        "granian",
        "--interface",
        interface,
        "--no-ws",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        str(workers),
    ]
    if backpressure is not None:
        args.extend(["--backpressure", str(backpressure)])
    if threads is not None:
        args.extend(["--blocking-threads", str(threads)])
    start_process(name, [*args, f"korfbal.{interface}:application"])
    return f"http://127.0.0.1:{port}"


def start_proxy(
    stack: ExitStack,
    options: argparse.Namespace,
    proxy_origin: str,
    origin: str,
    read_origins: tuple[str | None, str | None, str | None],
) -> str:
    """Start owned Caddy and return its immutable image identity."""
    live_origin, sse_origin, public_origin = read_origins
    config = options.output.resolve() / "Caddyfile"
    config.write_text(
        caddy_config(
            proxy_origin,
            origin,
            live_origin or origin,
            sse_origin or origin,
            public_origin or origin,
        )
    )
    name = f"korfbal-loadtest-proxy-{uuid4().hex[:12]}"
    proxy_id = command([
        DOCKER,
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        "korfbal.synthetic-loadtest=true",
        "--network",
        "host",
        *(["--cpuset-cpus", options.server_cpus] if options.server_cpus else []),
        "--mount",
        f"type=bind,source={config},target=/etc/caddy/Caddyfile,readonly",
        "caddy:2.10.2-alpine",
    ])
    stack.callback(
        subprocess.run,
        [DOCKER, "rm", "--force", "--volumes", proxy_id],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    return command([
        DOCKER,
        "inspect",
        "--format",
        "{{.Image}}",
        proxy_id,
    ])


def restrict_generator_cpus(stack: ExitStack, cpus: str | None) -> None:
    """Pin the writer, monitor and spawned viewers after servers have started."""
    if cpus:
        stack.callback(os.sched_setaffinity, 0, os.sched_getaffinity(0))
        os.sched_setaffinity(0, {int(cpu) for cpu in cpus.split(",")})


def allocate_origin() -> str:
    """Reserve an available loopback port for an owned service."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


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
        "proxy": "caddy:2.10.2-alpine" if options.proxy else None,
        "server_cpus": options.server_cpus,
        "generator_cpus": options.generator_cpus,
        "compact": options.compact,
        "generator_shards": options.generator_shards,
        "generator_source_addresses": "Separate loopback source addresses per shard",
        "python": sys.version.split()[0],
        "django": version("django"),
        "granian": version("granian"),
        "host_logical_cpus": os.cpu_count(),
        "asgi_workers": options.workers,
        "sse_workers": options.sse_workers,
        "sse_backpressure_per_worker": options.sse_backpressure,
        "public_workers": options.public_workers,
        "public_backpressure_per_worker": options.public_backpressure,
        "live_workers": options.live_workers,
        "live_threads_per_worker": options.live_threads,
        "live_backpressure_per_worker": options.live_backpressure,
        "live_interface": "wsgi" if options.live_workers else "asgi",
        "max_live_p95_ms": options.max_live_p95_ms,
        "max_push_p95_ms": options.max_push_p95_ms,
        "db_pool_size_per_web_worker": options.db_pool_size,
        "background_jobs": options.background_jobs,
        "initial_shots_per_match": options.shots,
        "max_command_p95_ms": options.max_p95_ms,
        "limitations": (
            "Local synthetic traffic; API/worker/generator share one physical host. "
            "No TLS, WAN, media or provider traffic. "
            "Optional local Caddy uses HTTP/1.1. "
            "CPU partitions, when specified, share host memory and networking. "
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
            options.server_cpus,
        )
        valkey, valkey_port = container(
            stack, "valkey/valkey:8.1.3-alpine", 6379, [], options.server_cpus
        )
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
        proxy_origin = allocate_origin() if options.proxy else None
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
            "KORFBAL_LOADTEST_ORIGIN": proxy_origin or origin,
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

        if options.profile_commands or options.profile_payloads:
            write_command_profile(
                options.output,
                fixtures[0],
                environment,
                metadata,
                payloads=options.profile_payloads,
            )
            return 0

        def start_process(name: str, args: list[str]) -> subprocess.Popen[Any]:
            log = stack.enter_context((options.output / f"{name}.log").open("w"))
            process_env = dict(environment)
            if name in {"server", "live-server", "sse-server", "public-server"}:
                process_env["KORFBAL_DB_POOL_MAX_SIZE"] = str(options.db_pool_size)
            process = subprocess.Popen(
                (
                    ["taskset", "--cpu-list", options.server_cpus]
                    if options.server_cpus
                    else []
                )
                + args,
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
        live_origin = start_read_pool(
            start_process,
            name="live-server",
            workers=options.live_workers,
            backpressure=options.live_backpressure,
            threads=options.live_threads,
        )
        sse_origin = start_read_pool(
            start_process,
            name="sse-server",
            workers=options.sse_workers,
            backpressure=options.sse_backpressure,
        )
        public_origin = start_read_pool(
            start_process,
            name="public-server",
            workers=options.public_workers,
            backpressure=options.public_backpressure,
            threads=options.live_threads,
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
        if proxy_origin:
            metadata["proxy_image"] = start_proxy(
                stack,
                options,
                proxy_origin,
                origin,
                (live_origin, sse_origin, public_origin),
            )
            origin = proxy_origin
            live_origin = sse_origin = public_origin = None
        restrict_generator_cpus(stack, options.generator_cpus)
        result = asyncio.run(
            experiment(
                origin,
                fixtures,
                options,
                db_port,
                (live_origin, sse_origin, public_origin),
            )
        )
        result["environment"] = metadata
        (options.output / "results.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
