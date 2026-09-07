"""Regressions for source aliases and poules without descriptive metadata."""

from django.utils import timezone
import pytest

from apps.club.models import Club as AppClub
from apps.competition.models import Club, Match, Pool, Team, TeamGroup
from apps.competition.services.identities import team_group_key
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.schedule.models import Season, SeasonPool
from apps.team.models import (
    Team as AppTeam,
    TeamData,
)


DISTINCT_IDENTITIES = 2


@pytest.mark.parametrize("designation", ["1", "2", "J1", "MW1"])
def test_joint_key_ignores_only_partner_order(designation: str) -> None:
    """Keep exact partners, team numbers and age categories significant."""
    assert team_group_key(f"Aurora/DKV (IJ) {designation}", "Aurora") == team_group_key(
        f"DKV (IJ)/Aurora {designation}", "Aurora"
    )
    assert team_group_key(f"DKV (IJ)/Aurora {designation}", "Other") != team_group_key(
        f"Aurora/DKV (IJ) {designation}", "Other"
    )
    assert team_group_key("Aurora/DKV (IJ) J1", "Aurora") != team_group_key(
        "Aurora/DKV (IJ) 1", "Aurora"
    )


@pytest.mark.django_db
def test_new_joint_variants_share_one_group(season: Season) -> None:
    """Indoor/outdoor registrations with reversed names share the native team."""
    importer = Importer(season, timezone.now())
    for identifier, name in [
        ("outdoor", "Aurora/DKV (IJ) 2"),
        ("indoor", "DKV (IJ)/Aurora 2"),
    ]:
        importer.apply(
            "club_teams",
            "aurora",
            {
                "ClubTeam": [
                    {
                        "PublicTeamId": identifier,
                        "TeamName": name,
                        "Club": {"ClubId": "aurora", "ClubName": "Aurora"},
                    }
                ]
            },
        )
    publish_catalogue()
    assert TeamGroup.objects.count() == 1
    assert AppTeam.objects.count() == 1
    assert TeamData.objects.count() == 1


@pytest.mark.django_db
def test_existing_unlinked_alias_merges_without_replacing_roster(
    season: Season,
) -> None:
    """Repair the conflict while preserving source variants and the native roster."""
    local_club = AppClub.objects.create(name="DKV (IJ)/Aurora")
    local_team = AppTeam.objects.create(club=local_club, name="2")
    roster = TeamData.objects.create(team=local_team, season=season)
    source = Club.objects.create(external_id="aurora", name="Aurora")
    linked = TeamGroup.objects.create(
        club=source,
        season=season,
        name="DKV (IJ)/Aurora 2",
        normalized_name="dkv (ij)/aurora 2",
        local_team=local_team,
        local_team_data=roster,
    )
    alias = TeamGroup.objects.create(
        club=source,
        season=season,
        name="Aurora/DKV (IJ) 2",
        normalized_name="aurora/dkv (ij) 2",
    )
    variant = Team.objects.create(
        club=source, season=season, name=alias.name, external_id="outdoor", group=alias
    )
    opponent = Importer(season, timezone.now()).team({
        "PublicTeamId": "opponent",
        "TeamName": "OKV 2",
        "Club": {"ClubId": "okv", "ClubName": "OKV"},
    })
    fixture = Match.objects.create(
        season=season,
        external_id="held-match",
        home_team=variant,
        away_team=opponent,
        starts_at=timezone.now(),
        status="SCHEDULED",
    )
    result = publish_catalogue()
    fixture.refresh_from_db()
    assert fixture.local_match_id is not None
    assert fixture.local_match.home_team_id == local_team.pk
    variant.refresh_from_db()
    linked.refresh_from_db()
    assert variant.group_id == linked.pk
    assert linked.local_team_data_id == roster.pk
    assert TeamData.objects.get(team=local_team).pk == roster.pk
    assert result["counts"]["source_groups_merged"] == 1
    assert not result["blocked"]
    assert not publish_catalogue()["blocked"]


@pytest.mark.django_db
def test_distinct_existing_native_teams_are_not_merged(season: Season) -> None:
    """Automatic source cleanup cannot combine independently linked native records."""
    club = AppClub.objects.create(name="Aurora")
    source = Club.objects.create(external_id="aurora", name="Aurora", local_club=club)
    for name in ["Aurora/DKV (IJ) 2", "DKV (IJ)/Aurora 2"]:
        local = AppTeam.objects.create(club=club, name=name)
        TeamGroup.objects.create(
            club=source,
            season=season,
            name=name,
            normalized_name=name.casefold(),
            local_team=local,
            local_team_data=TeamData.objects.create(team=local, season=season),
        )
    publish_catalogue()
    assert AppTeam.objects.count() == TeamGroup.objects.count() == DISTINCT_IDENTITIES


@pytest.mark.django_db
def test_unnamed_poules_remain_distinct_and_gain_metadata(season: Season) -> None:
    """Unique source IDs prevent empty names from claiming the same native poule."""
    legacy = SeasonPool.objects.create(season=season, name="", sport="ZA")
    first = Pool.objects.create(
        season=season, external_id="101222", sport="ZA", local_pool=legacy
    )
    second = Pool.objects.create(season=season, external_id="101234", sport="ZA")
    assert not publish_catalogue()["blocked"]
    legacy.refresh_from_db()
    second.refresh_from_db()
    assert legacy.name == "KNKV-poule 101222"
    assert second.local_pool_id != legacy.pk
    assert second.local_pool.name == "KNKV-poule 101234"
    second.name = "A1"
    second.class_name = "Senioren"
    second.save()
    assert not publish_catalogue()["blocked"]
    second.local_pool.refresh_from_db()
    assert second.local_pool.name == "Senioren A1"
    assert SeasonPool.objects.count() == DISTINCT_IDENTITIES
    first.refresh_from_db()
    assert first.local_pool_id == legacy.pk


@pytest.mark.django_db
def test_same_named_source_poules_do_not_merge(season: Season) -> None:
    """Different provider IDs can legitimately have identical descriptive labels."""
    for identifier in ["one", "two"]:
        Pool.objects.create(
            season=season, external_id=identifier, name="A1", sport="ZA"
        )
    assert not publish_catalogue()["blocked"]
    assert SeasonPool.objects.count() == DISTINCT_IDENTITIES
    assert not publish_catalogue()["blocked"]
