"""Profile synthetic commands separately from concurrent HTTP measurements."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
import cProfile
import json
from operator import itemgetter
import sys
from time import perf_counter
from types import CodeType
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.db import connection

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import MatchData
from apps.game_tracker.services import tracker_http
from apps.schedule.models import Match


COMMAND_COUNT = 20


def _measure(
    profiler: cProfile.Profile, match: Match, payload: dict[str, Any]
) -> dict[str, Any]:
    lock = tracker_http.locked_match_mutation
    sample: dict[str, Any] = {
        "command": payload["command"],
        "sql_calls": 0,
        "sql_ms": 0.0,
        "row_lock_sql_ms": 0.0,
    }

    def sql(
        execute: Callable[..., Any],
        statement: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        started = perf_counter()
        try:
            return execute(statement, params, many, context)
        finally:
            elapsed = (perf_counter() - started) * 1000
            sample["sql_calls"] += 1
            sample["sql_ms"] += elapsed
            if "FOR UPDATE" in statement:
                sample["row_lock_sql_ms"] += elapsed

    @contextmanager
    def timed_lock(match_data_id: object) -> Iterator[MatchData]:
        requested = perf_counter()
        with lock(match_data_id) as aggregate:
            acquired = perf_counter()
            sample["lock_acquire_ms"] = (acquired - requested) * 1000
            try:
                yield aggregate
            finally:
                sample["locked_body_ms"] = (perf_counter() - acquired) * 1000

    stages: dict[str, float] = {}

    def timed_stage(name: str, function: Callable[..., Any]) -> Callable[..., object]:
        def measured(*args: object, **kwargs: object) -> object:
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                stages[name] = stages.get(name, 0.0) + (perf_counter() - started) * 1000

        return measured

    with ExitStack() as stack:
        for name in (
            "_timeline_resource_payloads",
            "_register_tracker_command",
            "get_tracker_state",
            "record_match_change",
        ):
            stack.enter_context(
                patch.object(
                    tracker_http, name, timed_stage(name, getattr(tracker_http, name))
                )
            )
        stack.enter_context(connection.execute_wrapper(sql))
        stack.enter_context(
            patch.object(tracker_http, "locked_match_mutation", timed_lock)
        )
        started = perf_counter()
        profiler.runcall(
            apply_tracker_command, match, team=match.home_team, payload=payload
        )
        sample["total_ms"] = (perf_counter() - started) * 1000
    sample["stages_ms"] = {name: round(elapsed, 3) for name, elapsed in stages.items()}
    return {
        key: round(value, 3) if isinstance(value, float) else value
        for key, value in sample.items()
    }


def profile_commands(fixture: dict[str, Any]) -> dict[str, Any]:
    """Measure lock acquisition/body, SQL and named functions without user data.

    Raises:
        RuntimeError: The database is not the disposable load-test database.

    """
    if settings.DATABASES["default"]["NAME"] != "korfbal_loadtest":
        raise RuntimeError("Profiling requires the disposable load-test database.")
    match = Match.objects.select_related(
        "home_team__club", "away_team__club", "season"
    ).get(pk=fixture["match_id"])
    samples = []
    profiler = cProfile.Profile()
    for index in range(COMMAND_COUNT):
        revision = MatchData.objects.get(match_link=match).live_revision
        payload = {
            "command": "goal_reg" if index % 5 == 0 else "shot_reg",
            "command_id": str(uuid4()),
            "expected_revision": revision,
            "player_id": fixture["players"][index % len(fixture["players"])],
            "goal_type": fixture["goal_type"],
            "for_team": index % 3 != 0,
        }
        samples.append(_measure(profiler, match, payload))
    rows = []
    for entry in profiler.getstats():
        code = entry.code
        if (
            not isinstance(code, CodeType)
            or "/apps/game_tracker/" not in code.co_filename
        ):
            continue
        rows.append({
            "function": code.co_filename.split("/apps/game_tracker/", 1)[1]
            + f":{code.co_firstlineno}:{code.co_name}",
            "calls": entry.callcount,
            "primitive_calls": entry.callcount - entry.reccallcount,
            "own_ms": round(entry.inlinetime * 1000, 3),
            "cumulative_ms": round(entry.totaltime * 1000, 3),
        })
    return {
        "samples": samples,
        "functions": sorted(rows, key=itemgetter("cumulative_ms"), reverse=True),
        "limitations": (
            "Twenty sequential service calls; cProfile overhead is included. "
            "No HTTP, spectator load or worker consumption. "
            "Use direct stage timers for attribution; cProfile parent totals may "
            "be distorted by async publication callbacks. "
            "SQL and stage times overlap. Locked body excludes commit and "
            "post-commit callbacks; row-lock SQL includes roundtrip time, "
            "not just contention."
        ),
    }


def main() -> None:
    """Read only an isolated fixture from stdin and emit aggregate profile data."""
    print(json.dumps(profile_commands(json.load(sys.stdin))))
