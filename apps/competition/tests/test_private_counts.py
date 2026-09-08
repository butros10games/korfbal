"""Anonymous counts retain roster size without retaining private identities."""

from datetime import timedelta

from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from apps.competition.models import Match, SyncResource, Team
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.rosters import queue_rosters
from apps.competition.services.season_repair import repair
from apps.competition.tests.test_importer import match_payload
from apps.competition.tests.test_lineups import lineup
from apps.competition.tests.test_rosters import person
from apps.competition.tests.test_season_bindings import setup_variants
from apps.player.models import Player
from apps.schedule.models import Season


@pytest.mark.django_db
def test_counts_are_anonymous_deduplicated_and_season_scoped(season: Season) -> None:
    """Private people have no native profile, and staff never inflate player count."""
    importer, outdoor, indoor = setup_variants(season)
    repair(season, season.start_date.year)
    private = person("HIDDEN", "PRIVATE")
    staff = {
        **person("STAFF", "PRIVATE"),
        "TeamPersonFunction": {"RoleId": "COACHING_STAFF"},
    }
    unrelated = {**person("OTHER", "PRIVATE"), "TeamPerson": False}
    importer.apply(
        "team_roster",
        "OUT",
        {"TeamPersonOverview": [person(), private, private, staff, unrelated]},
    )
    outdoor.refresh_from_db()
    indoor.refresh_from_db()
    assert outdoor.private_roster_counts == {"players": 1, "staff": 1}
    assert not Player.all_objects.filter(
        knkv_person_id__in=["HIDDEN", "STAFF", "OTHER"]
    ).exists()
    client = APIClient()
    url = f"/api/team/teams/{outdoor.local_team_data.team_id}/overview/"
    response = client.get(url, {"season": str(season.pk)}).data
    assert response["private_roster"] == {
        "players": 1,
        "staff": 1,
        "is_estimate": False,
    }
    assert response["meta"]["roster_count"] == len(response["roster"]) + 1
    assert len(response["roster"]) == 1
    assert (
        client.get(url, {"season": str(indoor.local_team_data.season_id)}).data[
            "private_roster"
        ]["players"]
        == 0
    )


@pytest.mark.django_db
def test_private_counts_refresh_expire_and_reject_old_empty_snapshot(
    season: Season,
) -> None:
    """An empty/private-only response advances the checkpoint without identities."""
    now = timezone.now()
    importer, outdoor, _ = setup_variants(season)
    fresh = Importer(season, now + timedelta(seconds=1))
    fresh.apply(
        "team_roster", "OUT", {"TeamPersonOverview": [person("HIDDEN", "PRIVATE")]}
    )
    Importer(season, now).apply("team_roster", "OUT", {"TeamPersonOverview": []})
    outdoor.refresh_from_db()
    assert outdoor.private_roster_counts["players"] == 1
    Team.objects.filter(pk=outdoor.pk).update(
        roster_observed_at=now - timedelta(days=9)
    )
    url = f"/api/team/teams/{outdoor.local_team_data.team_id}/overview/"
    assert (
        APIClient()
        .get(url, {"season": str(season.pk)})
        .data["private_roster"]["players"]
        == 0
    )
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": []})
    outdoor.refresh_from_db()
    assert outdoor.private_roster_counts == {"players": 0, "staff": 0}


@pytest.mark.django_db
def test_private_public_transition_replaces_placeholder(season: Season) -> None:
    """A privacy transition changes the count rather than creating duplicate people."""
    importer, outdoor, _ = setup_variants(season)
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": [person()]})
    importer.apply(
        "team_roster", "OUT", {"TeamPersonOverview": [person(privacy="PRIVATE")]}
    )
    outdoor.refresh_from_db()
    assert outdoor.private_roster_counts["players"] == 1
    assert not outdoor.local_team_data.players.exists()
    importer.apply("team_roster", "OUT", {"TeamPersonOverview": [person()]})
    outdoor.refresh_from_db()
    assert outdoor.private_roster_counts["players"] == 0
    assert outdoor.local_team_data.players.count() == 1


@pytest.mark.django_db
def test_match_private_counts_are_not_added_to_team_roster(season: Season) -> None:
    """Keep each match's anonymous selection size separate from team membership."""
    importer = Importer(season, timezone.now())
    importer.match(match_payload(), result=True)
    publish_catalogue()
    data = lineup()
    data["HomeTeamPerson"] = [person("PRIVATE", "PRIVATE")]
    data["AwayTeamPerson"] = []
    importer.apply("match_lineup", "M1", data)
    fixture = Match.objects.get()
    assert fixture.private_lineup_counts == {
        "home": {"players": 1, "staff": 0},
        "away": {"players": 0, "staff": 0},
    }
    assert not Player.all_objects.exists()
    assert not fixture.home_team.private_roster_counts


@pytest.mark.django_db
def test_multiple_variant_counts_are_flagged_instead_of_summed(season: Season) -> None:
    """There is no identity-free way to calculate a distinct union across feeds."""
    importer, outdoor, indoor = setup_variants(season)
    outdoor.refresh_from_db()
    indoor.refresh_from_db()
    assert outdoor.local_team_data_id == indoor.local_team_data_id
    for source in ("OUT", "IN"):
        importer.apply(
            "team_roster", source, {"TeamPersonOverview": [person("HIDDEN", "PRIVATE")]}
        )
    outdoor.refresh_from_db()
    response = APIClient().get(
        f"/api/team/teams/{outdoor.local_team_data.team_id}/overview/?season={season.pk}"
    )
    assert response.data["private_roster"] == {
        "players": 1,
        "staff": 0,
        "is_estimate": True,
    }


@pytest.mark.django_db
def test_shared_private_sentinel_counts_each_roster_row(season: Season) -> None:
    """Show every anonymous player/staff row despite KNKV's shared PRIVATE ID."""
    importer, outdoor, indoor = setup_variants(season)
    repair(season, season.start_date.year)
    anonymous = person("PRIVATE", "PRIVATE")
    staff = {**anonymous, "TeamPersonFunction": {"RoleId": "COACHING_STAFF"}}
    known_private = person("REAL-HIDDEN-ID", "PRIVATE")
    importer.apply(
        "team_roster",
        "OUT",
        {
            "TeamPersonOverview": [
                anonymous,
                anonymous,
                anonymous,
                staff,
                staff,
                known_private,
                known_private,
                {**anonymous, "TeamPerson": False},
            ]
        },
    )
    outdoor.refresh_from_db()
    indoor.refresh_from_db()
    assert outdoor.private_roster_counts == {"players": 4, "staff": 2}
    assert not Player.all_objects.exists()
    url = f"/api/team/teams/{outdoor.local_team_data.team_id}/overview/"
    response = APIClient().get(url, {"season": str(season.pk)}).data
    assert response["private_roster"] == {
        "players": 4,
        "staff": 2,
        "is_estimate": False,
    }
    assert response["meta"]["roster_count"] == response["private_roster"]["players"]
    assert (
        APIClient()
        .get(url, {"season": str(indoor.local_team_data.season_id)})
        .data["private_roster"]["players"]
        == 0
    )


@pytest.mark.django_db
def test_shared_private_sentinel_counts_each_match_side(season: Season) -> None:
    """Never collapse anonymous selections into one person or across sides."""
    importer = Importer(season, timezone.now())
    importer.match(match_payload(), result=True)
    publish_catalogue()
    data = lineup()
    anonymous = person("PRIVATE", "PRIVATE")
    data["HomeTeamPerson"] = [anonymous, anonymous, anonymous]
    data["AwayTeamPerson"] = [anonymous, anonymous]
    importer.apply("match_lineup", "M1", data)
    assert Match.objects.get().private_lineup_counts == {
        "home": {"players": 3, "staff": 0},
        "away": {"players": 2, "staff": 0},
    }
    assert not Player.all_objects.exists()


@pytest.mark.django_db
def test_targeted_private_refresh_preserves_checkpoints_and_retries(
    season: Season,
) -> None:
    """Refetch successful private snapshots once, preserving failed and public feeds."""
    _, outdoor, indoor = setup_variants(season)
    Team.objects.filter(pk__in=[outdoor.pk, indoor.pk]).update(
        private_roster_counts={"players": 1, "staff": 0}
    )
    queue_rosters(season)
    now = timezone.now()
    SyncResource.objects.filter(kind="team_roster").update(fetched_at=now, etag="old")
    SyncResource.objects.filter(kind="team_roster", source_id="IN").update(failures=6)
    public = SyncResource.objects.create(
        season=season,
        kind="team_roster",
        source_id="PUBLIC",
        fetched_at=now,
        etag="keep",
        next_sync_at=now + timedelta(days=7),
    )
    assert queue_rosters(season, refresh_private=True) == 1
    refreshed = SyncResource.objects.get(kind="team_roster", source_id="OUT")
    assert refreshed.fetched_at is None
    assert not refreshed.etag
    failed = SyncResource.objects.get(kind="team_roster", source_id="IN")
    max_failures = 6
    assert failed.failures == max_failures
    assert failed.fetched_at == now
    public.refresh_from_db()
    assert public.fetched_at == now
    assert public.etag == "keep"
    assert queue_rosters(season, refresh_private=True) == 0
