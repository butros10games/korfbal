"""Public source-state projection preserves scores, ownership and bounded reads."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from apps.competition.models import Match
from apps.competition.queries.source_results import source_results
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import INDOOR, configure_seasons
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload
from apps.game_tracker.adapters.outbound.published_live_store import MAX_SNAPSHOT_AGE
from apps.game_tracker.composition import (
    published_live_store,
    read_public_live,
    read_public_match,
)
from apps.game_tracker.domain.source_results import normalize_source_result
from apps.game_tracker.models import MatchData, MatchPart, Shot
from apps.game_tracker.tests.tracker_test_helpers import create_tracker_player
from apps.schedule.api.serializers import MatchSerializer
from apps.schedule.models import (
    Match as NativeMatch,
    Season,
    SeasonPool,
)
from apps.team.models import Team as NativeTeam


def _fixture(
    season: Season,
    *,
    status: str = "SUSPENDED",
    scores: tuple[int | None, int | None] = (8, 5),
) -> Match:
    row = match_payload()
    row.update(
        Status=status,
        HomeResult={"Score": scores[0]},
        AwayResult={"Score": scores[1]},
    )
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    return Match.objects.get()


@pytest.mark.parametrize(
    ("status", "scores", "expected_status", "is_final"),
    [
        ("SUSPENDED", (8, 5), "suspended", False),
        ("SUSPENDED", (8, None), "suspended", False),
        ("SUSPENDED", (None, None), "suspended", False),
        ("FINAL", (0, 0), "final", True),
        ("FINAL", (8, None), "final", False),
        ("WITHDRAWN", (None, None), "cancelled", False),
        ("UNRECOGNIZED", (None, None), "unknown", False),
    ],
)
def test_source_normalization_never_invents_score_or_finality(
    status: str,
    scores: tuple[int | None, int | None],
    expected_status: str,
    is_final: bool,
) -> None:
    """Unknown sides remain null and only fully scored finals are final."""
    result = normalize_source_result(
        status=status,
        home_score=scores[0],
        away_score=scores[1],
        source="knkv",
        display_authority="provider",
    )
    assert result["status"] == expected_status
    assert result["score"] == {"home": scores[0], "away": scores[1]}
    assert result["is_final"] is is_final


@pytest.mark.django_db
@pytest.mark.parametrize("scores", [(8, 5), (8, None), (None, None), (0, 0)])
def test_suspended_result_reaches_detail_summary_and_live_without_tracker_history(
    season: Season, scores: tuple[int | None, int | None]
) -> None:
    """Public reads show source state while native tracking remains upcoming."""
    source = _fixture(season, scores=scores)
    native = source.local_match
    assert native is not None
    expected = {
        "status": "suspended",
        "score": {"home": scores[0], "away": scores[1]},
        "is_final": False,
        "source": "knkv",
        "display_authority": "provider",
    }
    client = APIClient()
    for route in ("", "summary/", "live/"):
        response = client.get(f"/api/matches/{native.pk}/{route}")
        assert response.status_code == HTTPStatus.OK
        assert response.json()["source_result"] == expected
        if route:
            assert response.json()["status"] == "upcoming"
            assert response.json()["score"] == {"home": 0, "away": 0}
    for route in (
        f"/api/club/clubs/{native.home_team.club_id}/overview/",
        f"/api/team/teams/{native.home_team_id}/overview/",
    ):
        response = client.get(route, {"season": str(season.pk)})
        assert response.status_code == HTTPStatus.OK
        assert response.json()["matches"]["upcoming"][0]["source_result"] == expected
        assert response.json()["matches"]["recent"] == []
    assert not MatchData.objects.filter(status="finished").exists()
    assert not Shot.objects.exists()
    assert not MatchPart.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "updates",
    [
        {"status": "active"},
        {"live_revision": 1},
        {"command_sequence": 1},
        {"event_sequence": 1},
        {"home_score": 9, "away_score": 2},
        {"status": "finished", "home_score": 9, "away_score": 2},
    ],
)
def test_tracked_or_manually_edited_result_keeps_local_display_authority(
    season: Season, updates: dict[str, Any]
) -> None:
    """The additive source observation cannot replace protected native work."""
    source = _fixture(season)
    MatchData.objects.filter(match_link_id=source.local_match_id).update(**updates)
    projection = source_results([str(source.local_match_id)])
    assert projection[str(source.local_match_id)]["display_authority"] == "local"
    assert projection[str(source.local_match_id)]["score"] == {"home": 8, "away": 5}
    if updates.get("status") == "finished":
        live = read_public_live(match_id=str(source.local_match_id))
        assert live is not None
        assert live["source_result"]["display_authority"] == "local"
        assert live["score"] == {"home": 9, "away": 2}


@pytest.mark.django_db
def test_season_routed_fixture_keeps_provider_authority(season: Season) -> None:
    """Indoor fixtures published into their bound season still show source state."""
    indoor = configure_seasons(season, 2026)[INDOOR]
    row = match_payload()
    row.update(Status="SUSPENDED", HomeResult={"Score": 8}, AwayResult={"Score": 5})
    row["HomeTeam"]["SportId"] = INDOOR
    row["AwayTeam"]["SportId"] = INDOOR
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Match.objects.get()
    assert source.local_match is not None
    assert source.local_match.season_id == indoor.pk != source.season_id
    match_id = str(source.local_match_id)
    assert source_results([match_id])[match_id]["display_authority"] == "provider"
    # A manual move to another season still awaits native reconciliation.
    NativeMatch.objects.filter(pk=source.local_match_id).update(season=season)
    assert source_results([match_id])[match_id]["display_authority"] == "local"


@pytest.mark.django_db
def test_pending_participant_correction_cannot_display_another_teams_result(
    season: Season,
) -> None:
    """Changed source sides remain descriptive until native linkage reconciles."""
    source = _fixture(season)
    Match.objects.filter(pk=source.pk).update(home_team_id=source.away_team_id)
    result = source_results([str(source.local_match_id)])[str(source.local_match_id)]
    assert result["display_authority"] == "local"


@pytest.mark.django_db
@pytest.mark.parametrize("side", ["home", "away"])
def test_canonical_group_correction_blocks_source_display_with_stale_season_pointer(
    season: Season, side: str
) -> None:
    """Public reads resolve participants through the publisher's canonical group."""
    source = _fixture(season)
    native = source.local_match
    assert native is not None
    team = source.home_team if side == "home" else source.away_team
    corrected_team = NativeTeam.objects.create(
        name=f"Corrected {side}", club=native.home_team.club
    )
    assert team.group is not None
    assert team.local_team_data is not None
    previous_team_id = team.local_team_data.team_id
    team.group.local_team = corrected_team
    team.group.save(update_fields=("local_team",))
    # Retained TeamData is now stale and cannot prove the corrected identity.
    assert team.local_team_data.team_id == previous_team_id
    client = APIClient()
    for route in ("", "summary/", "live/"):
        response = client.get(f"/api/matches/{native.pk}/{route}")
        assert response.status_code == HTTPStatus.OK
        assert response.json()["source_result"]["display_authority"] == "local"
        if route:
            assert response.json()["status"] == "upcoming"
            assert response.json()["score"] == {"home": 0, "away": 0}


@pytest.mark.django_db
def test_stale_teamdata_pointer_does_not_override_matching_canonical_participants(
    season: Season,
) -> None:
    """The source group, rather than a seasonal roster pointer, identifies a side."""
    source = _fixture(season)
    source.home_team.local_team_data = source.away_team.local_team_data
    source.home_team.save(update_fields=("local_team_data",))
    result = source_results([str(source.local_match_id)])[str(source.local_match_id)]
    assert result["display_authority"] == "provider"


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["kickoff", "pool", "season"])
def test_fixture_source_result_requires_same_participants_and_playing_season(
    season: Season, field: str
) -> None:
    """Compatible results remain separate from protected pool/kickoff changes."""
    source = _fixture(season, status="FINAL")
    native = source.local_match
    assert native is not None
    changes: dict[str, Any]
    if field == "kickoff":
        changes = {"start_time": native.start_time + timedelta(days=1)}
    elif field == "pool":
        pool = SeasonPool.objects.create(season=season, name="Manual pool")
        changes = {"pool_id": pool.pk}
    else:
        edited_season = Season.objects.create(
            name="Manually selected season",
            start_date=season.start_date + timedelta(days=365),
            end_date=season.end_date + timedelta(days=365),
        )
        changes = {"season_id": edited_season.pk}
    # Deliberately leave native score provenance/revision untouched. An edited
    # season changes sporting identity; pool/kickoff edits do not own the score.
    NativeMatch.objects.filter(pk=native.pk).update(**changes)
    Match.objects.filter(pk=source.pk).update(home_score=30, away_score=20)
    client = APIClient()
    for route in ("", "summary/", "live/"):
        response = client.get(f"/api/matches/{native.pk}/{route}")
        assert response.status_code == HTTPStatus.OK
        expected_authority = "local" if field == "season" else "provider"
        assert (
            response.json()["source_result"]["display_authority"] == expected_authority
        )
        assert response.json()["source_result"]["score"] == {"home": 30, "away": 20}
        if route:
            assert response.json()["status"] == "finished"
            assert response.json()["score"] == {"home": 8, "away": 5}


@pytest.mark.django_db
@pytest.mark.parametrize("scored", [True, False])
def test_finished_tracker_registrations_still_own_scores_with_stale_persisted_totals(
    season: Season, scored: bool
) -> None:
    """Registered goals or misses prevent a manual-score fallback."""
    source = _fixture(season)
    assert source.local_match is not None
    tracker = MatchData.objects.get(match_link=source.local_match)
    player = create_tracker_player(username="finished-source-tracker")
    Shot.objects.create(
        match_data=tracker,
        team=source.local_match.home_team,
        player=player,
        scored=scored,
    )
    MatchData.objects.filter(pk=tracker.pk).update(
        status="finished", score_source="tracker", home_score=99, away_score=88
    )
    live = read_public_live(match_id=str(source.local_match_id))
    assert live is not None
    assert live["score"] == {"home": int(scored), "away": 0}
    assert live["source_result"]["display_authority"] == "local"


@pytest.mark.django_db
def test_legacy_tracker_history_is_protected_even_without_a_revision(
    season: Season,
) -> None:
    """Legacy raw history cannot be mistaken for a pristine imported fixture."""
    source = _fixture(season)
    assert source.local_match is not None
    tracker = MatchData.objects.get(match_link=source.local_match)
    player = create_tracker_player(username="source-result-history")
    Shot.objects.create(
        match_data=tracker,
        team=source.local_match.home_team,
        player=player,
        scored=True,
    )
    MatchData.objects.filter(pk=tracker.pk).update(
        score_source="tracker", live_revision=0, command_sequence=0, event_sequence=0
    )
    Match.objects.filter(pk=source.pk).update(local_created=False, published_state={})
    result = source_results([str(source.local_match_id)])[str(source.local_match_id)]
    assert result["display_authority"] == "local"


@pytest.mark.django_db
def test_source_reads_and_match_list_serializer_are_bounded_and_batched(
    season: Season,
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """An extra catalogue row is excluded and list serialization reads once."""
    source = _fixture(season)
    Match.objects.create(
        season=season,
        external_id="unselected",
        home_team=source.home_team,
        away_team=source.away_team,
        starts_at=source.starts_at,
        status="FINAL",
        home_score=30,
        away_score=20,
    )
    # Fixtures plus one read of the selected scopes' season bindings.
    with django_assert_num_queries(2):
        assert set(source_results([str(source.local_match_id)])) == {
            str(source.local_match_id)
        }
    with django_assert_num_queries(0):
        assert source_results([]) == {}
    matches = list(
        NativeMatch.objects.select_related(
            "home_team__club", "away_team__club", "season", "pool"
        ).filter(pk=source.local_match_id)
    )
    with django_assert_num_queries(2):
        payload = MatchSerializer(matches, many=True).data
    assert payload[0]["source_result"]["score"] == {"home": 8, "away": 5}


@pytest.mark.django_db(transaction=True)
def test_expired_cache_refreshes_source_results_even_when_native_revision_unchanged(
    season: Season,
) -> None:
    """Unchanged polls and ordinary summary refetches carry new source scores."""
    source = _fixture(season)
    match_id = str(source.local_match_id)
    before = read_public_live(match_id=match_id)
    before_summary = read_public_match(match_id=match_id, resource="summary")
    assert before is not None
    assert before_summary is not None
    envelope = published_live_store.get(match_id)
    assert envelope is not None
    Match.objects.filter(pk=source.pk).update(home_score=9)
    # The first release retains the existing short shared cache lifetime.
    warm = read_public_live(match_id=match_id)
    assert warm is not None
    assert warm["source_result"] == before["source_result"]
    assert read_public_match(match_id=match_id, resource="summary") == before_summary
    with patch(
        "apps.game_tracker.adapters.outbound.published_live_store.time",
        return_value=envelope["created_at"] + MAX_SNAPSHOT_AGE + 1,
    ):
        poll = read_public_live(
            match_id=match_id, since_revision=before["live_revision"]
        )
        summary = read_public_match(match_id=match_id, resource="summary")
    assert poll is not None
    assert summary is not None
    assert poll["changed"] is False
    assert poll["live_revision"] == before["live_revision"]
    assert poll["source_result"]["score"] == {"home": 9, "away": 5}
    assert summary["status"] == "upcoming"
    assert summary["score"] == {"home": 0, "away": 0}
    assert summary["source_result"]["score"] == {"home": 9, "away": 5}


@pytest.mark.django_db
def test_unchanged_poll_in_caller_transaction_reads_source_without_score_or_clock(
    season: Season,
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """The metadata shortcut delivers same-revision corrections without a snapshot."""
    source = _fixture(season)
    native_id = str(source.local_match_id)
    tracker = MatchData.objects.get(match_link_id=source.local_match_id)
    Match.objects.filter(pk=source.pk).update(home_score=9)
    with (
        patch("apps.game_tracker.services.public_live._build_public_snapshot") as build,
        django_assert_num_queries(3),
    ):
        poll = read_public_live(
            match_id=native_id, since_revision=tracker.live_revision
        )
    build.assert_not_called()
    assert poll is not None
    assert poll["changed"] is False
    assert poll["live_revision"] == tracker.live_revision
    assert poll["source_result"]["score"] == {"home": 9, "away": 5}
