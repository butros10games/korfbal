"""Pool-first and match-first tournament administration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
import pytest

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentStandingAdjustment,
    TournamentTeam,
)
from apps.tournament.services.editing import create_pool
from apps.tournament.services.standings import calculate_pool_standings


pytestmark = pytest.mark.django_db
MANUAL_POOL_ORDER = 7
UPDATED_POOL_ORDER = 9
REORDERED_POOL_ORDER = 10
GENERATED_DURATION_MINUTES = 12
GENERATED_CHANGEOVER_MINUTES = 2
GENERATED_REST_MINUTES = 3


def _setup(client: Client) -> tuple[Tournament, list[TournamentTeam]]:
    owner = get_user_model().objects.create_user(username="planning-manager")
    client.force_login(owner)
    tournament = Tournament.objects.create(
        name="Planning admin",
        slug="planning-admin",
        owner=owner,
        starts_at=timezone.now(),
        timezone="Europe/Amsterdam",
    )
    teams = [
        TournamentTeam.objects.create(
            tournament=tournament,
            name=f"Team {index}",
            seed=index,
        )
        for index in range(1, 5)
    ]
    TournamentField.objects.create(tournament=tournament, label="Veld 1")
    return tournament, teams


def test_pool_and_match_generation_are_separate_review_steps(client: Client) -> None:
    """Generating pools leaves them editable until matches are generated separately."""
    tournament, _ = _setup(client)
    expected_count = 2

    pools_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/generate/",
        data={"pool_count": 2, "strategy": "snake"},
        content_type="application/json",
    )

    assert pools_response.status_code == HTTPStatus.OK
    assert tournament.pools.count() == expected_count
    assert tournament.matches.count() == 0
    pool_ids = set(tournament.pools.values_list("id_uuid", flat=True))

    matches_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/generate/",
        data={
            "legs": 1,
            "duration_minutes": GENERATED_DURATION_MINUTES,
            "changeover_minutes": GENERATED_CHANGEOVER_MINUTES,
            "minimum_rest_minutes": GENERATED_REST_MINUTES,
        },
        content_type="application/json",
    )

    assert matches_response.status_code == HTTPStatus.OK
    assert tournament.matches.count() == expected_count
    assert set(tournament.pools.values_list("id_uuid", flat=True)) == pool_ids
    tournament.refresh_from_db()
    assert tournament.match_duration_minutes == GENERATED_DURATION_MINUTES
    assert tournament.changeover_minutes == GENERATED_CHANGEOVER_MINUTES
    assert tournament.minimum_rest_minutes == GENERATED_REST_MINUTES


def test_manual_pool_and_match_creation_feed_the_same_snapshot(client: Client) -> None:
    """Organizer-created records use the normal snapshot and remain editable."""
    tournament, teams = _setup(client)
    field_id = str(tournament.fields.get().id_uuid)
    pool_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/",
        data={
            "name": "Poule Handmatig",
            "sort_order": MANUAL_POOL_ORDER,
            "assigned_field_id": field_id,
            "team_ids": [str(teams[0].id_uuid), str(teams[1].id_uuid)],
        },
        content_type="application/json",
    )

    assert pool_response.status_code == HTTPStatus.CREATED
    assert pool_response.json()["pools"][0]["sort_order"] == MANUAL_POOL_ORDER
    assert pool_response.json()["pools"][0]["assigned_field"] == {
        "id_uuid": field_id,
        "label": "Veld 1",
    }
    pool_id = pool_response.json()["pools"][0]["id_uuid"]
    create_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/",
        data={
            "pool_id": pool_id,
            "home_team_id": str(teams[0].id_uuid),
            "away_team_id": str(teams[1].id_uuid),
            "field_id": field_id,
            "date": "2027-06-12",
            "start_time": "09:00",
            "duration_minutes": 20,
            "round_number": 1,
        },
        content_type="application/json",
    )

    assert create_response.status_code == HTTPStatus.CREATED
    match = tournament.matches.get()
    update_response = client.patch(
        f"/api/tournaments/{tournament.id_uuid}/matches/{match.id_uuid}/",
        data={"start_time": "09:30"},
        content_type="application/json",
    )

    assert update_response.status_code == HTTPStatus.OK
    match.refresh_from_db()
    assert match.starts_at.isoformat() == "2027-06-12T07:30:00+00:00"

    delete_response = client.delete(
        f"/api/tournaments/{tournament.id_uuid}/matches/{match.id_uuid}/"
    )
    assert delete_response.status_code == HTTPStatus.NO_CONTENT

    update_pool_response = client.patch(
        f"/api/tournaments/{tournament.id_uuid}/pools/{pool_id}/",
        data={
            "sort_order": UPDATED_POOL_ORDER,
            "team_ids": [str(teams[0].id_uuid), str(teams[2].id_uuid)],
        },
        content_type="application/json",
    )
    assert update_pool_response.status_code == HTTPStatus.OK
    assert (
        update_pool_response.json()["pools"][0]["assigned_field"]["id_uuid"] == field_id
    )
    assert update_pool_response.json()["pools"][0]["sort_order"] == UPDATED_POOL_ORDER
    assert {
        row["team_id"] for row in update_pool_response.json()["pools"][0]["standings"]
    } == {str(teams[0].id_uuid), str(teams[2].id_uuid)}


@pytest.mark.parametrize("change", ["rename", "field", "reseed", "replace-team"])
def test_pool_edits_preserve_retained_memberships_and_adjustments(
    client: Client, change: str
) -> None:
    """Pool edits must not erase penalties or bonuses for teams that stay in it."""
    tournament, teams = _setup(client)
    pool = create_pool(tournament, name="A", team_ids=[team.pk for team in teams[:3]])
    entries = {entry.team_id: entry for entry in pool.entries.all()}
    penalty = TournamentStandingAdjustment.objects.create(
        entry=entries[teams[0].pk],
        points=-1,
        reason="Late arrival",
        created_by=tournament.owner,
    )
    bonus = TournamentStandingAdjustment.objects.create(
        entry=entries[teams[1].pk],
        points=1,
        reason="Fair play",
        created_by=tournament.owner,
    )
    expected_teams = teams[:3]
    values: dict[str, object]
    if change == "rename":
        values = {"name": "Renamed"}
    elif change == "field":
        values = {"assigned_field_id": str(tournament.fields.get().pk)}
    else:
        expected_teams = [
            teams[1],
            teams[0],
            teams[3 if change == "replace-team" else 2],
        ]
        values = {"team_ids": [str(team.pk) for team in expected_teams]}
    original_adjustments = list(
        TournamentStandingAdjustment.objects.order_by("pk").values()
    )

    response = client.patch(
        f"/api/tournaments/{tournament.pk}/pools/{pool.pk}/",
        values,
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert list(TournamentStandingAdjustment.objects.order_by("pk").values()) == (
        original_adjustments
    )
    assert list(pool.entries.values_list("team_id", flat=True)) == [
        team.pk for team in expected_teams
    ]
    for team in expected_teams:
        if team.pk in entries:
            assert pool.entries.get(team=team).pk == entries[team.pk].pk
    standings = {
        row["team_id"]: row for row in response.json()["pools"][0]["standings"]
    }
    assert standings[str(teams[0].pk)]["points"] == penalty.points
    assert standings[str(teams[1].pk)]["points"] == bonus.points


def test_manual_pool_requires_at_least_two_teams(client: Client) -> None:
    """A pool cannot be saved in a state the match generator cannot schedule."""
    tournament, teams = _setup(client)

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/",
        data={"name": "Poule Alleen", "team_ids": [str(teams[0].id_uuid)]},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "at least 2" in response.json()["team_ids"][0]
    assert tournament.pools.count() == 0


def _refereed_schedule(
    client: Client,
) -> tuple[Tournament, list[TournamentTeam], TournamentMatch, TournamentField]:
    tournament, teams = _setup(client)
    teams.append(TournamentTeam.objects.create(tournament=tournament, name="Team 5"))
    pool = create_pool(tournament, name="A", team_ids=[team.pk for team in teams])
    other_field = TournamentField.objects.create(tournament=tournament, label="Veld 2")
    match = TournamentMatch.objects.create(
        tournament=tournament,
        stage=pool.stage,
        pool=pool,
        home_team=teams[0],
        away_team=teams[1],
        referee_team=teams[2],
        field=tournament.fields.get(label="Veld 1"),
        match_number=1,
        starts_at=datetime(2027, 6, 12, 7, tzinfo=UTC),
        duration_minutes=20,
        round_number=1,
    )
    return tournament, teams, match, other_field


@pytest.mark.parametrize("assignment", ["home", "away", "referee", "unused"])
def test_delete_team_preserves_scheduled_assignments(
    client: Client, assignment: str
) -> None:
    """A referee-only team is still in use; only an unassigned team can be deleted."""
    tournament, teams, match, _ = _refereed_schedule(client)
    team = {
        "home": teams[0],
        "away": teams[1],
        "referee": teams[2],
        "unused": teams[3],
    }[assignment]
    revision = tournament.live_revision

    response = client.delete(f"/api/tournaments/{tournament.pk}/teams/{team.pk}/")

    tournament.refresh_from_db()
    if assignment == "unused":
        assert response.status_code == HTTPStatus.NO_CONTENT
        assert not TournamentTeam.objects.filter(pk=team.pk).exists()
        assert tournament.live_revision == revision + 1
    else:
        assert response.status_code == HTTPStatus.CONFLICT
        assert TournamentTeam.objects.filter(pk=team.pk).exists()
        assert tournament.live_revision == revision
    match.refresh_from_db()
    assert (match.home_team_id, match.away_team_id, match.referee_team_id) == tuple(
        team.pk for team in teams[:3]
    )


@pytest.mark.parametrize("editing", [False, True], ids=["create", "reschedule"])
def test_manual_match_respects_players_referee_duties(
    client: Client, editing: bool
) -> None:
    """Teams cannot be scheduled to play during a previously assigned referee duty."""
    tournament, teams, duty, field = _refereed_schedule(client)
    existing = None
    url = f"/api/tournaments/{tournament.pk}/matches/"
    if editing:
        existing = TournamentMatch.objects.create(
            tournament=tournament,
            stage=duty.stage,
            pool=duty.pool,
            home_team=teams[2],
            away_team=teams[3],
            field=field,
            match_number=2,
            starts_at=duty.starts_at + timedelta(hours=1),
            duration_minutes=20,
        )
        url += f"{existing.pk}/"
    count = tournament.matches.count()
    response = (client.patch if editing else client.post)(
        url,
        {
            "pool_id": str(duty.pool_id),
            "home_team_id": str(teams[2].pk),
            "away_team_id": str(teams[3].pk),
            "field_id": str(field.pk),
            "date": "2027-06-12",
            "start_time": "09:00",
            "duration_minutes": 20,
            "round_number": 1,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert tournament.matches.count() == count
    if existing:
        existing.refresh_from_db()
        assert existing.starts_at == duty.starts_at + timedelta(hours=1)


@pytest.mark.parametrize("role", ["playing", "refereeing"])
def test_manual_reschedule_revalidates_its_referee(client: Client, role: str) -> None:
    """Moving a match must respect its referee team's other commitments."""
    tournament, teams, match, field = _refereed_schedule(client)
    TournamentMatch.objects.create(
        tournament=tournament,
        stage=match.stage,
        pool=match.pool,
        home_team=teams[2] if role == "playing" else teams[4],
        away_team=teams[3],
        referee_team=teams[2] if role == "refereeing" else None,
        field=field,
        match_number=2,
        starts_at=match.starts_at + timedelta(hours=1),
        duration_minutes=20,
    )
    before = match.starts_at
    response = client.patch(
        f"/api/tournaments/{tournament.pk}/matches/{match.pk}/",
        {"start_time": "10:00"},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CONFLICT
    match.refresh_from_db()
    assert match.starts_at == before


def test_manual_match_cannot_turn_its_referee_into_a_participant(
    client: Client,
) -> None:
    """Changing entrants cannot leave a playing team assigned to referee itself."""
    tournament, teams, match, _ = _refereed_schedule(client)
    response = client.patch(
        f"/api/tournaments/{tournament.pk}/matches/{match.pk}/",
        {"home_team_id": str(teams[2].pk)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CONFLICT
    match.refresh_from_db()
    assert match.home_team_id == teams[0].pk


@pytest.mark.parametrize("cancelled", [False, True], ids=["adjacent", "cancelled"])
def test_manual_match_allows_available_referee_teams(
    client: Client, cancelled: bool
) -> None:
    """Finished time slots and cancelled duties do not block a team's next match."""
    tournament, teams, duty, field = _refereed_schedule(client)
    if cancelled:
        duty.status = TournamentMatch.Status.CANCELLED
        duty.save(update_fields=["status"])
    response = client.post(
        f"/api/tournaments/{tournament.pk}/matches/",
        {
            "pool_id": str(duty.pool_id),
            "home_team_id": str(teams[2].pk),
            "away_team_id": str(teams[3].pk),
            "field_id": str(field.pk),
            "date": "2027-06-12",
            "start_time": "09:00" if cancelled else "09:20",
            "duration_minutes": 20,
            "round_number": 2,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CREATED


@pytest.mark.parametrize("change", ["team", "field", "time", "duration", "unchanged"])
def test_manual_match_edits_invalidate_old_readiness(
    client: Client, change: str
) -> None:
    """Readiness belongs to the entrants, field and time the referee inspected."""
    tournament, teams, match, other_field = _refereed_schedule(client)
    match.field_ready_at = timezone.now()
    match.field_ready_by = tournament.owner
    match.field_ready_by_name = "Ready referee"
    match.save()
    before_revision = match.revision
    ready_at = match.field_ready_at
    changes = {
        "team": {"home_team_id": str(teams[3].pk)},
        "field": {"field_id": str(other_field.pk)},
        "time": {"start_time": "09:30"},
        "duration": {"duration_minutes": 30},
        "unchanged": {"start_time": "09:00"},
    }
    response = client.patch(
        f"/api/tournaments/{tournament.pk}/matches/{match.pk}/",
        changes[change],
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    match.refresh_from_db()
    if change == "unchanged":
        assert match.field_ready_at == ready_at
        assert match.revision == before_revision
    else:
        assert match.field_ready_at is None
        assert match.field_ready_by_id is None
        assert not match.field_ready_by_name
        assert match.revision == before_revision + 1


def test_manual_match_must_use_the_pool_assigned_field(client: Client) -> None:
    """Manual planning cannot silently violate a pool's fixed field."""
    tournament, teams = _setup(client)
    assigned_field = tournament.fields.get()
    other_field = TournamentField.objects.create(
        tournament=tournament,
        label="Veld 2",
    )
    pool_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/",
        data={
            "name": "Poule A",
            "assigned_field_id": str(assigned_field.id_uuid),
            "team_ids": [str(team.id_uuid) for team in teams],
        },
        content_type="application/json",
    )

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/",
        data={
            "pool_id": pool_response.json()["pools"][0]["id_uuid"],
            "home_team_id": str(teams[0].id_uuid),
            "away_team_id": str(teams[1].id_uuid),
            "field_id": str(other_field.id_uuid),
            "date": "2027-06-12",
            "start_time": "09:00",
            "duration_minutes": 20,
            "round_number": 1,
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "assigned to field" in response.json()["detail"]


def test_pool_order_remains_editable_after_matches_are_generated(
    client: Client,
) -> None:
    """Presentation order can be repaired without rebuilding the schedule."""
    tournament, _ = _setup(client)
    generated = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/generate/",
        data={"pool_count": 2, "strategy": "snake"},
        content_type="application/json",
    )
    pool_a_id = generated.json()["pools"][0]["id_uuid"]
    matches = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/generate/",
        data={"legs": 1},
        content_type="application/json",
    )
    match_ids = {match["id_uuid"] for match in matches.json()["matches"]}

    reordered = client.patch(
        f"/api/tournaments/{tournament.id_uuid}/pools/{pool_a_id}/",
        data={"sort_order": REORDERED_POOL_ORDER},
        content_type="application/json",
    )

    assert reordered.status_code == HTTPStatus.OK
    assert [pool["name"] for pool in reordered.json()["pools"]] == [
        "Poule B",
        "Poule A",
    ]
    assert [pool["sort_order"] for pool in reordered.json()["pools"]] == [
        1,
        REORDERED_POOL_ORDER,
    ]
    assert {
        str(match_id)
        for match_id in tournament.matches.values_list("id_uuid", flat=True)
    } == match_ids


def _substitution_schedule(
    client: Client,
) -> tuple[Tournament, list[TournamentTeam], list[TournamentMatch]]:
    tournament, teams = _setup(client)
    teams.extend([
        TournamentTeam.objects.create(
            tournament=tournament,
            name=f"Team {index}",
            seed=index,
        )
        for index in range(5, 7)
    ])
    field_id = str(tournament.fields.get().id_uuid)
    pool_ids = []
    for name, pool_teams in (
        ("Poule A", teams[:3]),
        ("Poule B", teams[3:]),
    ):
        response = client.post(
            f"/api/tournaments/{tournament.id_uuid}/pools/",
            data={
                "name": name,
                "team_ids": [str(team.id_uuid) for team in pool_teams],
            },
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.CREATED
        pool_ids.append(response.json()["pools"][-1]["id_uuid"])

    for home, away, start_time in (
        (teams[0], teams[1], "09:00"),
        (teams[2], teams[0], "09:30"),
    ):
        response = client.post(
            f"/api/tournaments/{tournament.id_uuid}/matches/",
            data={
                "pool_id": pool_ids[0],
                "home_team_id": str(home.id_uuid),
                "away_team_id": str(away.id_uuid),
                "field_id": field_id,
                "date": "2027-06-12",
                "start_time": start_time,
                "duration_minutes": 20,
                "round_number": 1,
            },
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.CREATED
    return tournament, teams, list(tournament.matches.order_by("starts_at"))


def _referee_duty_for_absent_team(
    tournament: Tournament,
    teams: list[TournamentTeam],
    matches: list[TournamentMatch],
) -> TournamentMatch:
    pool = tournament.pools.get(name="Poule B")
    assert matches[1].starts_at is not None
    return TournamentMatch.objects.create(
        tournament=tournament,
        stage=pool.stage,
        pool=pool,
        home_team=teams[3],
        away_team=teams[4],
        referee_team=teams[0],
        referee_name="Existing claim",
        field=tournament.fields.get(),
        starts_at=matches[1].starts_at + timedelta(minutes=30),
        duration_minutes=20,
        round_number=1,
        match_number=3,
    )


def test_absent_team_matches_can_be_filled_from_other_pools(client: Client) -> None:
    """A manager can atomically assign a different guest to every open fixture."""
    tournament, teams, matches = _substitution_schedule(client)
    referee_duty = _referee_duty_for_absent_team(tournament, teams, matches)
    referee_revision = referee_duty.revision
    first_revision = matches[0].revision
    matches[0].field_ready_at = timezone.now()
    matches[0].field_ready_by_name = "Ready referee"
    matches[0].save(update_fields=["field_ready_at", "field_ready_by_name"])

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/teams/{teams[0].id_uuid}/substitutions/",
        data={
            "replacements": [
                {
                    "match_id": str(matches[0].id_uuid),
                    "substitute_team_id": str(teams[3].id_uuid),
                },
                {
                    "match_id": str(matches[1].id_uuid),
                    "substitute_team_id": str(teams[4].id_uuid),
                },
            ],
            "referee_replacements": [
                {
                    "match_id": str(referee_duty.id_uuid),
                    "substitute_team_id": str(teams[5].id_uuid),
                }
            ],
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    teams[0].refresh_from_db()
    assert teams[0].withdrawn is True
    matches[0].refresh_from_db()
    matches[1].refresh_from_db()
    assert matches[0].home_team_id == teams[3].id_uuid
    assert matches[1].away_team_id == teams[4].id_uuid
    assert matches[0].field_ready_at is None
    assert not matches[0].field_ready_by_name
    assert matches[0].revision == first_revision + 1
    referee_duty.refresh_from_db()
    assert referee_duty.referee_team_id == teams[5].id_uuid
    assert not referee_duty.referee_name
    assert referee_duty.revision == referee_revision + 1
    response_matches = response.json()["matches"]
    assert response_matches[0]["home_is_guest"] is True
    assert response_matches[0]["away_is_guest"] is False
    assert response_matches[1]["home_is_guest"] is False
    assert response_matches[1]["away_is_guest"] is True

    tournament.matches.update(
        status=TournamentMatch.Status.FINAL,
        home_score=4,
        away_score=3,
    )
    standings = calculate_pool_standings(tournament.pools.get(name="Poule A"))
    assert all(row["played"] == 0 for row in standings)


def test_absent_team_replacement_requires_every_referee_duty(client: Client) -> None:
    """A referee assignment cannot be left behind when a team is withdrawn."""
    tournament, teams, matches = _substitution_schedule(client)
    referee_duty = _referee_duty_for_absent_team(tournament, teams, matches)

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/teams/{teams[0].id_uuid}/substitutions/",
        data={
            "replacements": [
                {
                    "match_id": str(matches[0].id_uuid),
                    "substitute_team_id": str(teams[3].id_uuid),
                },
                {
                    "match_id": str(matches[1].id_uuid),
                    "substitute_team_id": str(teams[4].id_uuid),
                },
            ]
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "referee duty" in response.json()["detail"]
    referee_duty.refresh_from_db()
    teams[0].refresh_from_db()
    assert referee_duty.referee_team_id == teams[0].id_uuid
    assert teams[0].withdrawn is False


def test_team_with_only_a_referee_duty_can_be_replaced(client: Client) -> None:
    """A team remains selectable when its only remaining task is refereeing."""
    tournament, teams, matches = _substitution_schedule(client)
    referee_duty = _referee_duty_for_absent_team(tournament, teams, matches)
    referee_duty.referee_team = teams[5]
    referee_duty.save(update_fields=["referee_team"])

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/teams/{teams[5].id_uuid}/substitutions/",
        data={
            "replacements": [],
            "referee_replacements": [
                {
                    "match_id": str(referee_duty.id_uuid),
                    "substitute_team_id": str(teams[2].id_uuid),
                }
            ],
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    teams[5].refresh_from_db()
    referee_duty.refresh_from_db()
    assert teams[5].withdrawn is True
    assert referee_duty.referee_team_id == teams[2].id_uuid


def test_absent_team_replacement_plan_is_complete_and_cross_pool(
    client: Client,
) -> None:
    """An invalid row rolls back the whole last-minute replacement plan."""
    tournament, teams, matches = _substitution_schedule(client)

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/teams/{teams[0].id_uuid}/substitutions/",
        data={
            "replacements": [
                {
                    "match_id": str(matches[0].id_uuid),
                    "substitute_team_id": str(teams[3].id_uuid),
                },
                {
                    "match_id": str(matches[1].id_uuid),
                    "substitute_team_id": str(teams[1].id_uuid),
                },
            ]
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "another pool" in response.json()["detail"]
    teams[0].refresh_from_db()
    matches[0].refresh_from_db()
    matches[1].refresh_from_db()
    assert teams[0].withdrawn is False
    assert matches[0].home_team_id == teams[0].id_uuid
    assert matches[1].away_team_id == teams[0].id_uuid


def test_absent_team_replacement_rejects_schedule_overlap(client: Client) -> None:
    """A guest cannot be assigned while it is already playing elsewhere."""
    tournament, teams, matches = _substitution_schedule(client)
    second_field = TournamentField.objects.create(
        tournament=tournament,
        label="Veld 2",
    )
    pool_b = tournament.pools.get(name="Poule B")
    TournamentMatch.objects.create(
        tournament=tournament,
        stage=pool_b.stage,
        pool=pool_b,
        home_team=teams[3],
        away_team=teams[4],
        field=second_field,
        starts_at=matches[0].starts_at,
        duration_minutes=20,
        round_number=1,
        match_number=3,
    )

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/teams/{teams[0].id_uuid}/substitutions/",
        data={
            "replacements": [
                {
                    "match_id": str(matches[0].id_uuid),
                    "substitute_team_id": str(teams[3].id_uuid),
                },
                {
                    "match_id": str(matches[1].id_uuid),
                    "substitute_team_id": str(teams[5].id_uuid),
                },
            ]
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "already playing or refereeing" in response.json()["detail"]


def test_match_editor_only_allows_an_existing_guest_team(client: Client) -> None:
    """The normal editor cannot bypass the atomic substitution workflow."""
    tournament, teams, matches = _substitution_schedule(client)
    match_url = f"/api/tournaments/{tournament.id_uuid}/matches/{matches[0].id_uuid}/"

    response = client.patch(
        match_url,
        data={"home_team_id": str(teams[3].id_uuid)},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "selected pool" in response.json()["detail"]

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/teams/{teams[0].id_uuid}/substitutions/",
        data={
            "replacements": [
                {
                    "match_id": str(matches[0].id_uuid),
                    "substitute_team_id": str(teams[3].id_uuid),
                },
                {
                    "match_id": str(matches[1].id_uuid),
                    "substitute_team_id": str(teams[4].id_uuid),
                },
            ]
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK

    response = client.patch(
        match_url,
        data={"home_team_id": str(teams[5].id_uuid)},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    matches[0].refresh_from_db()
    assert matches[0].home_team_id == teams[5].id_uuid
