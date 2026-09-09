"""Regression contracts for replacing and versioning persisted statistics."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import Mock

from django.core.exceptions import ValidationError
from django.db import (
    IntegrityError,
    OperationalError,
    connection,
    connections,
    transaction,
)
from django.utils import timezone
import pytest

from apps.club.services.admin import create_active_membership
from apps.game_tracker.models import MatchData, PlayerMatchImpact, PlayerMatchMinutes
from apps.game_tracker.services import (
    match_impact_persistence as impacts,
    match_minutes as minutes,
)
from apps.game_tracker.services.match_impact_scorer import MatchImpactRow
from apps.game_tracker.services.statistics_revision import (
    StatisticsRevisionChangedError,
    publish_statistics_revision,
)
from apps.game_tracker.services.tracker_commands.base import TrackerCommandError
from apps.game_tracker.services.tracker_commands.registry import (
    COMMAND_DEFINITIONS,
    command_definition,
)
from apps.player.models import Player, PlayerClubMembership, PlayerSong
from apps.player.services.player_settings import delete_player_profile
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamRosterMembership
from apps.team.tests.team_test_support import TeamTestContext, build_team_context


pytestmark = pytest.mark.django_db


@pytest.fixture
def context() -> tuple[TeamTestContext, MatchData]:
    """Build a match with a seasonal roster and a distinct opponent."""
    ctx = build_team_context()
    opponent = Team.objects.create(club=ctx.club, name="Opponent")
    match = Match.objects.create(
        home_team=ctx.team,
        away_team=opponent,
        season=ctx.season,
        start_time=timezone.now(),
    )
    return ctx, MatchData.objects.get(match_link=match)


@pytest.mark.parametrize("replacement", ["empty", "zero", "other"])
def test_minutes_replace_obsolete_players(
    context: tuple[TeamTestContext, MatchData],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    """A corrected result removes old rows without deleting other versions."""
    ctx, match = context
    PlayerMatchMinutes.objects.create(
        match_data=match, player=ctx.player, algorithm_version="v1", minutes_played=30
    )
    PlayerMatchMinutes.objects.create(
        match_data=match, player=ctx.player, algorithm_version="old", minutes_played=30
    )
    computed = (
        {}
        if replacement == "empty"
        else {
            str(ctx.player.pk if replacement == "zero" else ctx.coach.pk): 0
            if replacement == "zero"
            else 20
        }
    )
    monkeypatch.setattr(
        minutes, "compute_minutes_by_player_id", lambda **kwargs: computed
    )
    minutes.persist_match_minutes(match_data=match)
    assert not PlayerMatchMinutes.objects.filter(
        match_data=match, player=ctx.player, algorithm_version="v1"
    ).exists()
    assert (
        PlayerMatchMinutes.objects.filter(
            match_data=match, algorithm_version="old"
        ).count()
        == 1
    )
    if replacement == "other":
        row = PlayerMatchMinutes.objects.get(match_data=match, player=ctx.coach)
        assert row.minutes_played == Decimal(20)
        assert row.source_revision == match.live_revision


@pytest.mark.parametrize("kind", ["minutes", "impacts"])
def test_stale_computation_cannot_publish(
    context: tuple[TeamTestContext, MatchData],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A tracker edit during computation invalidates the whole publication."""
    ctx, match = context

    def compute(**kwargs: object) -> dict[str, int] | list[MatchImpactRow]:
        MatchData.objects.filter(pk=match.pk).update(
            live_revision=match.live_revision + 1
        )
        return (
            {str(ctx.player.pk): 20}
            if kind == "minutes"
            else [MatchImpactRow(str(ctx.player.pk), str(ctx.team.pk), Decimal(3))]
        )

    if kind == "minutes":
        monkeypatch.setattr(minutes, "compute_minutes_by_player_id", compute)
        persist = minutes.persist_match_minutes
    else:
        monkeypatch.setattr(impacts, "compute_match_impact_rows", compute)
        persist = impacts.persist_match_impact_rows
    with pytest.raises(StatisticsRevisionChangedError):
        persist(match_data=match)
    assert not PlayerMatchMinutes.objects.filter(match_data=match).exists()
    assert not PlayerMatchImpact.objects.filter(match_data=match).exists()


def test_hidden_identity_and_algorithm_versions_are_retained(
    context: tuple[TeamTestContext, MatchData], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Display eligibility cannot remove historical computation identities."""
    ctx, match = context
    Player.all_objects.filter(pk=ctx.player.pk).update(
        user=None, knkv_person_id="hidden", knkv_privacy="PRIVATE"
    )
    assert not Player.objects.filter(pk=ctx.player.pk).exists()
    rows = [MatchImpactRow(str(ctx.player.pk), str(ctx.team.pk), Decimal(3))]
    monkeypatch.setattr(impacts, "compute_match_impact_rows", lambda **kwargs: rows)
    for version in ("v7", "v8"):
        assert (
            impacts.persist_match_impact_rows(
                match_data=match, algorithm_version=version
            )
            == 1
        )
    assert set(
        PlayerMatchImpact.objects.filter(match_data=match).values_list(
            "algorithm_version", flat=True
        )
    ) == {"v7", "v8"}


def test_profile_deletion_preserves_history_and_closes_roster(
    context: tuple[TeamTestContext, MatchData],
) -> None:
    """Referenced sporting identity remains private after removing profile data."""
    ctx, match = context
    PlayerMatchMinutes.objects.create(
        match_data=match, player=ctx.player, minutes_played=30
    )
    user = ctx.player.user
    delete_player_profile(ctx.player)
    retained = Player.all_objects.get(pk=ctx.player.pk)
    assert retained.user_id is None
    assert not retained.name
    assert retained.display_name == "Afgeschermd"
    assert not Player.objects.filter(pk=retained.pk).exists()
    assert PlayerMatchMinutes.objects.get(player=retained).minutes_played == Decimal(30)
    assert not ctx.team_data.players.filter(pk=retained.pk).exists()
    assert not TeamRosterMembership.objects.filter(
        player=retained, ended_at=None
    ).exists()
    assert type(user).objects.filter(pk=user.pk).exists()


def test_account_deletion_preserves_history(
    context: tuple[TeamTestContext, MatchData],
) -> None:
    """Django's account deletion collector cannot cascade into sporting records."""
    ctx, match = context
    PlayerMatchMinutes.objects.create(
        match_data=match, player=ctx.player, minutes_played=30
    )
    ctx.player.user.delete()
    assert Player.all_objects.get(pk=ctx.player.pk).archived_at is not None
    assert PlayerMatchMinutes.objects.filter(player_id=ctx.player.pk).exists()


def test_roster_records_rejoining_and_multiple_roles(
    context: tuple[TeamTestContext, MatchData],
) -> None:
    """Rejoining opens a new interval while other roles retain their history."""
    ctx, _match = context
    ctx.team_data.staff.add(ctx.player)
    ctx.team_data.players.remove(ctx.player)
    ctx.team_data.players.add(ctx.player)
    rows = TeamRosterMembership.objects.filter(player=ctx.player)
    expected_intervals = 2
    assert rows.filter(role="players").count() == expected_intervals
    assert rows.filter(role="players", ended_at=None).count() == 1
    assert rows.filter(role="staff", ended_at=None).count() == 1


def test_database_rejects_invalid_schedule_and_scores(
    context: tuple[TeamTestContext, MatchData],
) -> None:
    """Direct ORM writes cannot bypass same-row domain invariants."""
    ctx, match = context
    for model, pk, changes in (
        (Match, match.match_link_id, {"away_team_id": ctx.team.pk}),
        (
            Season,
            ctx.season.pk,
            {
                "end_date": ctx.season.start_date.replace(
                    year=ctx.season.start_date.year - 1
                )
            },
        ),
        (MatchData, match.pk, {"home_score": -1}),
        (MatchData, match.pk, {"parts": 0}),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.filter(pk=pk).update(**changes)


def test_song_deletion_removes_every_ordered_reference(
    context: tuple[TeamTestContext, MatchData],
) -> None:
    """Direct song deletion cascades references while retaining selection order."""
    ctx, _match = context
    first = PlayerSong.objects.create(player=ctx.player)
    second = PlayerSong.objects.create(player=ctx.player)
    ctx.player.goal_song_song_ids = [str(second.pk), str(first.pk)]
    ctx.player.save(update_fields=["goal_song_song_ids"])
    ctx.team_data.fallback_goal_song_song_ids = [str(first.pk), str(second.pk)]
    ctx.team_data.save(update_fields=["fallback_goal_song_song_ids"])
    first.delete()
    ctx.player.refresh_from_db()
    ctx.team_data.refresh_from_db()
    assert ctx.player.goal_song_song_ids == [str(second.pk)]
    assert ctx.team_data.fallback_goal_song_song_ids == [str(second.pk)]


def test_impact_cache_is_scoped_to_input_revision(
    context: tuple[TeamTestContext, MatchData],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrected match cannot reuse a breakdown from its previous revision."""
    _ctx, match = context
    compute = Mock(return_value=([], {}))
    monkeypatch.setattr(impacts, "compute_match_impact_breakdown", compute)
    impacts.compute_match_impact_breakdown_cached(match_data=match)
    impacts.compute_match_impact_breakdown_cached(match_data=match)
    assert compute.call_count == 1
    MatchData.objects.filter(pk=match.pk).update(live_revision=match.live_revision + 1)
    impacts.compute_match_impact_breakdown_cached(match_data=match)
    expected_computations = 2
    assert compute.call_count == expected_computations


def test_club_membership_rejects_overlapping_closed_interval(
    context: tuple[TeamTestContext, MatchData],
) -> None:
    """Service writes reject date overlap even when the prior interval is closed."""
    ctx, _match = context
    today = timezone.localdate()
    PlayerClubMembership.objects.create(
        player=ctx.player,
        club=ctx.club,
        start_date=today - timedelta(days=10),
        end_date=today,
    )
    with pytest.raises(ValidationError, match="overlaps"):
        create_active_membership(club=ctx.club, player=ctx.player, start_date=today)
    membership, created = create_active_membership(
        club=ctx.club, player=ctx.player, start_date=today + timedelta(days=1)
    )
    assert created
    assert membership.start_date > today


def test_tracker_command_contract_matches_shared_client_fixtures() -> None:
    """Keep accepted command families and payload rules aligned across runtimes."""
    cases = json.loads(
        (
            Path(__file__).resolve().parents[6]
            / "fixtures/korfbal/tracker-commands.json"
        ).read_text()
    )
    assert {entry["payload"]["command"] for entry in cases if entry["valid"]} == {
        definition.name for definition in COMMAND_DEFINITIONS
    }
    for entry in cases:
        payload = entry["payload"]
        if entry["valid"]:
            command_definition(payload).parse(payload)
        else:
            with pytest.raises(TrackerCommandError):
                command_definition(payload).parse(payload)


@pytest.mark.django_db(transaction=True)
def test_statistics_publication_locks_match_on_postgres(
    context: tuple[TeamTestContext, MatchData],
) -> None:
    """Another writer cannot acquire the aggregate lock during publication."""
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock regression")
    _ctx, match = context

    def competing_writer() -> None:
        try:
            with transaction.atomic():
                MatchData.objects.select_for_update(nowait=True).get(pk=match.pk)
        finally:
            connections["default"].close()

    with (
        publish_statistics_revision(match),
        ThreadPoolExecutor(max_workers=1) as pool,
        pytest.raises(OperationalError),
    ):
        pool.submit(competing_writer).result(timeout=10)
