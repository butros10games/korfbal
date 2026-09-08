"""Season repair, multi-role staff and native identity preservation."""

from datetime import date

from django.test import override_settings
from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from apps.competition.models import (
    Match,
    Pool,
    RosterMembership,
    SeasonBinding,
    SyncResource,
    Team,
)
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.season_repair import preview, repair
from apps.competition.services.seasons import INDOOR, OUTDOOR, native_season_filter
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.competition.tests.test_rosters import person
from apps.game_tracker.models import MatchData
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import TeamData


def setup_variants(season: Season) -> tuple[Importer, Team, Team]:
    """Publish two synthetic variants sharing one global team."""
    importer = Importer(season, timezone.now())
    outdoor_payload = team_payload("OUT")
    outdoor_payload["SportId"] = OUTDOOR
    indoor_payload = team_payload("IN")
    indoor_payload["SportId"] = INDOOR
    outdoor = importer.team(outdoor_payload)
    indoor = importer.team(indoor_payload)
    indoor.group = outdoor.group
    indoor.save()
    publish_catalogue()
    return importer, outdoor, indoor


@pytest.mark.django_db
def test_separate_rosters_repair_and_repeated_import(season: Season) -> None:
    """Moving source-owned links preserves global UUIDs and native manual players."""
    importer, outdoor, indoor = setup_variants(season)
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": [person("P1")]})
    importer.apply("team_roster", "IN", {"TeamPersonOverview": [person("P2")]})
    outdoor.refresh_from_db()
    old_data = outdoor.local_team_data
    manual = Player.objects.create(name="Native player")
    old_data.players.add(manual)
    original_ids = set(Player.objects.values_list("pk", flat=True))
    before = preview(season)
    assert not SeasonBinding.objects.exists()
    result = repair(season, season.start_date.year)
    assert result["blocked"] == []
    indoor.refresh_from_db()
    assert indoor.local_team_data_id != outdoor.local_team_data_id
    assert set(old_data.players.values_list("knkv_person_id", flat=True)) == {
        "P1",
        None,
    }
    assert list(
        indoor.local_team_data.players.values_list("knkv_person_id", flat=True)
    ) == ["P2"]
    assert set(Player.objects.values_list("pk", flat=True)) == original_ids
    assert before["teams"][INDOOR] == 1
    repair(season, season.start_date.year)
    importer.apply("team_roster", "IN", {"TeamPersonOverview": [person("P2")]})
    expected_two = 2
    assert old_data.players.count() == expected_two
    assert indoor.local_team_data.players.count() == 1
    assert TeamData.objects.filter(team=old_data.team).count() == expected_two
    response = APIClient().get(
        f"/api/team/teams/{old_data.team_id}/overview/?season={indoor.local_team_data.season_id}"
    )
    assert [p["id_uuid"] for p in response.data["roster"]] == [
        str(Player.objects.get(knkv_person_id="P2").pk)
    ]


@pytest.mark.django_db
def test_staff_roles_are_not_player_appearances(season: Season) -> None:
    """Staff reuse native profiles, coach membership and the dedicated UI payload."""
    importer, outdoor, _ = setup_variants(season)
    staff = person("STAFF")
    staff["TeamPersonFunction"] = {"RoleId": "COACHING_STAFF"}
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": [staff]})
    outdoor.refresh_from_db()
    data = outdoor.local_team_data
    assert not data.players.exists()
    assert data.staff.count() == 1
    assert data.coach.count() == 1
    response = APIClient().get(
        f"/api/team/teams/{data.team_id}/overview/?season={season.pk}"
    )
    assert response.data["roster"] == []
    assert response.data["stats"]["players"] == []
    assert response.data["staff"][0]["role_labels"] == ["Technische staf"]
    # Dual roles coexist; changing roles retires only importer-owned links.
    importer.apply(
        "team_roster", "OUT", {"TeamPersonOverview": [person("STAFF"), staff]}
    )
    assert data.players.count() == data.staff.count() == 1
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": [person("STAFF")]})
    assert data.players.count() == 1
    assert not data.staff.exists()
    assert not data.coach.exists()


@pytest.mark.django_db
def test_private_staff_removed(season: Season) -> None:
    """Withdrawal removes imported staff and coach links as well as the identity."""
    importer, outdoor, _ = setup_variants(season)
    row = person("STAFF")
    row["TeamPersonFunction"] = {"RoleId": "COACHING_STAFF"}
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": [row]})
    row["PrivacyLevel"] = "PRIVATE"
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": [row]})
    outdoor.refresh_from_db()
    assert not outdoor.local_team_data.staff.exists()
    assert not outdoor.local_team_data.coach.exists()
    assert not RosterMembership.objects.exists()


@pytest.mark.django_db
@override_settings(SPORTLINK_SPLIT_SEASONS=True)
def test_new_edition_creates_native_seasons_once() -> None:
    """A newly configured provider edition needs no manual indoor season creation."""
    scope = Season.objects.create(
        name="Outdoor 2027", start_date=date(2027, 8, 1), end_date=date(2028, 7, 31)
    )
    importer = Importer(scope, timezone.now())
    payload = team_payload("NEXT")
    payload["SportId"] = INDOOR
    importer.team(payload)
    importer.team(payload)
    assert Season.objects.filter(name="Zaal seizoen 2027-2028").count() == 1
    expected_two = 2
    assert SeasonBinding.objects.filter(scope=scope).count() == expected_two


@pytest.mark.django_db
def test_refresh_preserves_retry_cap(season: Season) -> None:
    """Role refresh is explicit and never resets failed feeds or photo checkpoints."""
    setup_variants(season)
    feed = SyncResource.objects.create(
        season=season,
        kind="team_roster",
        source_id="OUT",
        failures=6,
        next_sync_at=timezone.now(),
    )
    repair(season, season.start_date.year, refresh_rosters=True)
    feed.refresh_from_db()
    limit = 6
    assert feed.failures == limit


@pytest.mark.django_db
def test_indoor_poule_and_match_move_without_losing_ids_or_scores(
    season: Season,
) -> None:
    """Native fixtures and classes follow indoor mapping without a result reimport."""
    payload = match_payload()
    payload["HomeTeam"]["SportId"] = INDOOR
    payload["AwayTeam"]["SportId"] = INDOOR
    payload["Pool"]["ClassName"] = "1e klasse"
    importer = Importer(season, timezone.now())
    importer.apply("club_results", "C", {"MatchResult": [payload]})
    publish_catalogue()
    source = Match.objects.get()
    native_id = source.local_match_id
    pool_id = Pool.objects.get().local_pool_id
    score = MatchData.objects.get(match_link_id=native_id)
    saved = (score.pk, score.home_score, score.away_score)
    result = repair(season, season.start_date.year)
    assert result["blocked"] == []
    source.refresh_from_db()
    pool = Pool.objects.select_related("local_pool", "competition_class__edition").get()
    target = SeasonBinding.objects.get(scope=season, sport=INDOOR).season_id
    assert source.local_match_id == native_id
    assert source.local_match.season_id == target
    assert pool.local_pool_id == pool_id
    assert pool.local_pool.season_id == target
    assert pool.competition_class.edition.season_id == target
    score.refresh_from_db()
    assert (score.pk, score.home_score, score.away_score) == saved
    # A subsequent normal publication must not relink to the fetch scope.
    publish_catalogue()
    source.refresh_from_db()
    assert source.local_match_id == native_id
    assert source.local_match.season_id == target


@pytest.mark.django_db
def test_native_season_filter_separates_source_variants(season: Season) -> None:
    """Read models select disciplines without changing source scope IDs."""
    _, outdoor, indoor = setup_variants(season)
    repair(season, season.start_date.year)
    target = SeasonBinding.objects.get(scope=season, sport=INDOOR).season_id
    assert list(
        Team.objects.filter(native_season_filter(target)).values_list("pk", flat=True)
    ) == [indoor.pk]
    assert list(
        Team.objects.filter(native_season_filter(season.pk)).values_list(
            "pk", flat=True
        )
    ) == [outdoor.pk]
