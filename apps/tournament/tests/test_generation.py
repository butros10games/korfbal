"""Pool allocation and scheduling regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.tournament.models import Tournament, TournamentField, TournamentTeam
from apps.tournament.services.generation import (
    GenerationError,
    GenerationOptions,
    allocate_pools,
    build_generation_plan,
    round_robin_rounds,
)


pytestmark = pytest.mark.django_db


def _tournament() -> Tournament:
    user = get_user_model().objects.create_user(
        username="organizer",
        email="organizer@example.test",
        password="test-pass",
    )
    return Tournament.objects.create(
        name="KWT Zomertoernooi",
        slug="kwt-zomertoernooi",
        owner=user,
        starts_at=timezone.now(),
    )


@pytest.mark.parametrize("team_count", [4, 5, 8, 9])
def test_round_robin_contains_each_pair_once(team_count: int) -> None:
    """Odd and even team counts produce all unique pairings without self-play."""
    team_ids = [f"team-{index}" for index in range(team_count)]
    pairings = [
        pair for round_items in round_robin_rounds(team_ids) for pair in round_items
    ]
    normalized = {frozenset(pair) for pair in pairings}

    assert len(pairings) == team_count * (team_count - 1) // 2
    assert len(normalized) == len(pairings)
    assert all(home != away for home, away in pairings)


def test_snake_allocation_balances_pool_sizes_and_seeds() -> None:
    """Snake distribution keeps pool sizes within one and reverses alternate rows."""
    tournament = _tournament()
    teams = [
        TournamentTeam.objects.create(
            tournament=tournament,
            name=f"Team {index}",
            seed=index,
        )
        for index in range(1, 9)
    ]

    pools = allocate_pools(teams, pool_count=2, strategy="snake", random_seed=1)

    assert [[team.seed for team in pool] for pool in pools] == [
        [1, 4, 5, 8],
        [2, 3, 6, 7],
    ]


def test_pool_allocation_rejects_one_team_pools() -> None:
    """Automatic generation never reports success with pools that cannot play."""
    tournament = _tournament()
    teams = [
        TournamentTeam.objects.create(
            tournament=tournament,
            name=f"Team {index}",
            seed=index,
        )
        for index in range(1, 4)
    ]

    with pytest.raises(GenerationError, match="at least two teams"):
        allocate_pools(teams, pool_count=2, strategy="snake", random_seed=1)


def test_schedule_uses_fields_without_team_or_field_overlap() -> None:
    """Generated starts respect field capacity and minimum team rest."""
    tournament = _tournament()
    for index in range(1, 7):
        TournamentTeam.objects.create(
            tournament=tournament,
            name=f"Team {index}",
            seed=index,
        )
    for index in range(1, 3):
        TournamentField.objects.create(
            tournament=tournament,
            label=f"Veld {index}",
            sort_order=index,
        )

    plan = build_generation_plan(
        tournament,
        options=GenerationOptions(
            pool_count=1,
            duration_minutes=20,
            changeover_minutes=5,
            minimum_rest_minutes=10,
        ),
    )

    field_windows: dict[str, list[tuple[datetime, datetime]]] = {}
    team_windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for match in plan["matches"]:
        start = datetime.fromisoformat(match["starts_at"])
        end = start + timedelta(minutes=match["duration_minutes"])
        field_windows.setdefault(match["field_id"], []).append((start, end))
        for team_key in ("home_team_id", "away_team_id"):
            team_windows.setdefault(match[team_key], []).append((start, end))

    for windows in field_windows.values():
        ordered = sorted(windows)
        assert all(left[1] <= right[0] for left, right in pairwise(ordered))
    for windows in team_windows.values():
        ordered = sorted(windows)
        assert all(
            left[1] + timedelta(minutes=10) <= right[0]
            for left, right in pairwise(ordered)
        )
