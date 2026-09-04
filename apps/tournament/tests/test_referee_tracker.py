"""Permission, concurrency, and publication coverage for the referee tracker."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import call, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
import pytest
import qrcode

from apps.club.models.club import Club
from apps.schedule.models import Season
from apps.team.models import Team, TeamData
from apps.tournament.composition import change_publisher
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentMember,
    TournamentResultAudit,
    TournamentStage,
    TournamentTeam,
)


pytestmark = pytest.mark.django_db
OnCommitCapture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
REVISION_AFTER_HOME_GOAL = 2
REVISION_AFTER_GOAL_REMOVAL = 4
EXPECTED_GOAL_AUDITS = 2
EXPECTED_TOURNAMENT_REVISION = 3
EXPECTED_PDF_DUTIES = 2


def _match_graph() -> tuple[
    object,
    object,
    Tournament,
    TournamentField,
    TournamentField,
    TournamentMatch,
    TournamentMatch,
]:
    user_model = get_user_model()
    manager = user_model.objects.create_user(username="referee-manager")
    referee = user_model.objects.create_user(username="field-referee")
    tournament = Tournament.objects.create(
        name="Referee Cup",
        slug="referee-cup",
        owner=manager,
        starts_at=timezone.now(),
        status=Tournament.Status.PUBLISHED,
    )
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Pools",
        kind=TournamentStage.Kind.POOL,
    )
    field_one = TournamentField.objects.create(tournament=tournament, label="Field 1")
    field_two = TournamentField.objects.create(tournament=tournament, label="Field 2")
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=f"Team {index}")
        for index in range(1, 5)
    ]
    match_one = TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        field=field_one,
        home_team=teams[0],
        away_team=teams[1],
        match_number=1,
    )
    match_two = TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        field=field_two,
        home_team=teams[2],
        away_team=teams[3],
        match_number=2,
    )
    TournamentMember.objects.create(
        tournament=tournament,
        user=referee,
        role=TournamentMember.Role.SCOREKEEPER,
        field=field_one,
    )
    return manager, referee, tournament, field_one, field_two, match_one, match_two


def test_referee_tracker_requires_authentication_and_assigned_field(
    client: Client,
) -> None:
    """The focused tracker remains private and respects field scope."""
    _, referee, _, _, _, allowed_match, denied_match = _match_graph()

    anonymous = client.get(f"/api/tournaments/matches/{allowed_match.id_uuid}/tracker/")
    assert anonymous.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    assert "Location" not in anonymous.headers

    client.force_login(referee)
    allowed = client.get(f"/api/tournaments/matches/{allowed_match.id_uuid}/tracker/")
    denied = client.get(f"/api/tournaments/matches/{denied_match.id_uuid}/tracker/")

    assert allowed.status_code == HTTPStatus.OK
    assert allowed.json()["match"]["field"]["label"] == "Field 1"
    assert allowed.json()["match"]["home_team"]["name"] == "Team 1"
    assert denied.status_code == HTTPStatus.FORBIDDEN


def test_referee_marks_ready_and_each_goal_is_published_once(
    client: Client,
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """Readiness and score commands advance revisions without duplicate goals."""
    _, referee, tournament, _, _, match, _ = _match_graph()
    client.force_login(referee)
    ready_url = f"/api/tournaments/matches/{match.id_uuid}/tracker/ready/"
    goal_url = f"/api/tournaments/matches/{match.id_uuid}/tracker/goal/"

    capture_callbacks = django_capture_on_commit_callbacks
    with (
        patch.object(change_publisher, "publish") as publish,
        capture_callbacks(execute=True),
    ):
        ready = client.post(
            ready_url,
            data={"expected_revision": 0},
            content_type="application/json",
        )
        repeated_ready = client.post(
            ready_url,
            data={"expected_revision": 0},
            content_type="application/json",
        )
        home_goal = client.post(
            goal_url,
            data={"side": "home", "expected_revision": 1},
            content_type="application/json",
        )
        stale_retry = client.post(
            goal_url,
            data={"side": "home", "expected_revision": 1},
            content_type="application/json",
        )
        away_goal = client.post(
            goal_url,
            data={"side": "away", "expected_revision": 2},
            content_type="application/json",
        )

    assert ready.status_code == HTTPStatus.OK
    assert ready.json()["match"]["field_ready_at"] is not None
    assert ready.json()["match"]["status"] == TournamentMatch.Status.SCHEDULED
    assert ready.json()["match"]["revision"] == 1
    assert repeated_ready.status_code == HTTPStatus.OK
    assert repeated_ready.json()["match"]["revision"] == 1
    assert home_goal.status_code == HTTPStatus.OK
    assert home_goal.json()["match"]["home_score"] == 1
    assert home_goal.json()["match"]["away_score"] == 0
    assert home_goal.json()["match"]["status"] == TournamentMatch.Status.LIVE
    assert home_goal.json()["match"]["revision"] == REVISION_AFTER_HOME_GOAL
    assert stale_retry.status_code == HTTPStatus.CONFLICT
    assert stale_retry.json()["state"]["match"]["home_score"] == 1
    assert away_goal.status_code == HTTPStatus.OK
    assert away_goal.json()["match"]["home_score"] == 1
    assert away_goal.json()["match"]["away_score"] == 1
    assert (
        TournamentResultAudit.objects.filter(match=match).count()
        == EXPECTED_GOAL_AUDITS
    )

    match.refresh_from_db()
    tournament.refresh_from_db()
    assert match.field_ready_by_id == referee.pk
    assert tournament.status == Tournament.Status.LIVE
    assert tournament.live_revision == EXPECTED_TOURNAMENT_REVISION
    assert publish.call_args_list == [
        call(tournament_id=str(tournament.id_uuid), revision=1),
        call(tournament_id=str(tournament.id_uuid), revision=2),
        call(tournament_id=str(tournament.id_uuid), revision=3),
    ]
    public_match = client.get(f"/api/tournaments/public/{tournament.slug}/").json()[
        "matches"
    ][0]
    assert public_match["home_score"] == 1
    assert public_match["away_score"] == 1
    assert public_match["revision"] == away_goal.json()["match"]["revision"]


def test_referee_removes_only_the_latest_visible_goal(client: Client) -> None:
    """A misclick can be rolled back once without overwriting a newer event."""
    _, referee, _, _, _, match, _ = _match_graph()
    client.force_login(referee)
    tracker_url = f"/api/tournaments/matches/{match.id_uuid}/tracker/"
    ready_url = f"{tracker_url}ready/"
    goal_url = f"{tracker_url}goal/"
    latest_url = f"{tracker_url}events/latest/"

    client.post(
        ready_url,
        data={"expected_revision": 0},
        content_type="application/json",
    )
    client.post(
        goal_url,
        data={"side": "home", "expected_revision": 1},
        content_type="application/json",
    )
    away_goal = client.post(
        goal_url,
        data={"side": "away", "expected_revision": 2},
        content_type="application/json",
    )

    assert away_goal.status_code == HTTPStatus.OK
    latest_event = away_goal.json()["latest_event"]
    assert latest_event["side"] == "away"
    assert latest_event["team_name"] == "Team 2"

    removed = client.delete(
        latest_url,
        data={"event_id": latest_event["id_uuid"], "expected_revision": 3},
        content_type="application/json",
    )

    assert removed.status_code == HTTPStatus.OK
    assert removed.json()["match"]["home_score"] == 1
    assert removed.json()["match"]["away_score"] == 0
    assert removed.json()["match"]["revision"] == REVISION_AFTER_GOAL_REMOVAL
    assert removed.json()["latest_event"]["side"] == "home"

    stale_repeat = client.delete(
        latest_url,
        data={"event_id": latest_event["id_uuid"], "expected_revision": 4},
        content_type="application/json",
    )
    assert stale_repeat.status_code == HTTPStatus.CONFLICT
    assert stale_repeat.json()["state"]["match"]["away_score"] == 0
    assert list(
        TournamentResultAudit.objects
        .filter(match=match)
        .order_by("created_at")
        .values_list("source", flat=True)
    ) == [
        TournamentResultAudit.Source.REFEREE_GOAL,
        TournamentResultAudit.Source.REFEREE_GOAL,
        TournamentResultAudit.Source.REFEREE_UNDO,
    ]


def test_referee_goal_requires_readiness_and_open_match(client: Client) -> None:
    """Goals cannot bypass readiness or alter a finalized result."""
    manager, _, _, _, _, match, _ = _match_graph()
    client.force_login(manager)
    goal_url = f"/api/tournaments/matches/{match.id_uuid}/tracker/goal/"

    before_ready = client.post(
        goal_url,
        data={"side": "away", "expected_revision": 0},
        content_type="application/json",
    )
    assert before_ready.status_code == HTTPStatus.CONFLICT
    assert before_ready.json()["state"]["match"]["away_score"] is None

    match.status = TournamentMatch.Status.FINAL
    match.home_score = 3
    match.away_score = 2
    match.save(update_fields=["status", "home_score", "away_score"])
    final_ready = client.post(
        f"/api/tournaments/matches/{match.id_uuid}/tracker/ready/",
        data={"expected_revision": 0},
        content_type="application/json",
    )
    assert final_ready.status_code == HTTPStatus.CONFLICT
    assert TournamentResultAudit.objects.filter(match=match).count() == 0


def test_public_snapshot_includes_operational_readiness_without_actor(
    client: Client,
) -> None:
    """Displays receive readiness while the referee identity stays private."""
    _, referee, tournament, _, _, match, _ = _match_graph()
    home_team = match.home_team
    assert home_team is not None
    home_team.color = "#123456"
    home_team.save(update_fields=["color"])
    match.field_ready_at = timezone.now()
    match.field_ready_by = referee
    match.save(update_fields=["field_ready_at", "field_ready_by"])

    response = client.get(f"/api/tournaments/public/{tournament.slug}/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()["matches"][0]
    assert payload["field_ready_at"] is not None
    assert payload["home_team"]["color"] == "#123456"
    assert "field_ready_by" not in payload


@override_settings(
    ALLOWED_HOSTS=["api.korfbal.butrosgroot.com", "testserver"],
    KORFBAL_ORIGIN="https://api.korfbal.butrosgroot.com",
    WEB_APP_ORIGIN="https://korfbal.localhost",
    WEB_KORFBAL_ORIGIN="https://korfbal.butrosgroot.com",
)
def test_manager_assigns_team_and_generates_shared_duty_qr(client: Client) -> None:
    """Managers can display one QR for every duty assigned to a team."""
    manager, _, tournament, _, _, match, _ = _match_graph()
    duty_team = tournament.teams.exclude(
        pk__in=(match.home_team_id, match.away_team_id)
    ).first()
    assert duty_team is not None
    client.force_login(manager)

    with patch.object(
        TournamentMatch.objects,
        "select_for_update",
        wraps=TournamentMatch.objects.select_for_update,
    ) as select_for_update:
        assignment = client.patch(
            f"/api/tournaments/{tournament.id_uuid}/matches/{match.id_uuid}/referee-duty/",
            data={"team_id": str(duty_team.id_uuid)},
            content_type="application/json",
        )
    with patch(
        "apps.tournament.api.views.qrcode.make",
        wraps=qrcode.make,
    ) as make_qr:
        qr = client.get(
            f"/api/tournaments/{tournament.id_uuid}/referee-teams/{duty_team.id_uuid}/qr/",
            headers={"host": "api.korfbal.butrosgroot.com"},
        )

    assert assignment.status_code == HTTPStatus.OK
    assigned_match = next(
        row
        for row in assignment.json()["matches"]
        if row["id_uuid"] == str(match.id_uuid)
    )
    assert assigned_match["referee_team"]["id_uuid"] == str(duty_team.id_uuid)
    assert qr.status_code == HTTPStatus.OK
    assert qr.json()["qr_data_url"].startswith("data:image/svg+xml;base64,")
    select_for_update.assert_called_once_with(of=("self",))
    duty_team.refresh_from_db()
    assert duty_team.referee_access_token is not None
    assert make_qr.call_args.args[0] == (
        "https://korfbal.butrosgroot.com/tournaments/referee/"
        f"{duty_team.referee_access_token}"
    )


def test_manager_cannot_assign_a_team_that_is_playing_at_the_same_time(
    client: Client,
) -> None:
    """A referee duty cannot overlap one of the team's own fixtures."""
    manager, _, tournament, _, _, match, other_match = _match_graph()
    duty_team = other_match.home_team
    assert duty_team is not None
    starts_at = timezone.now()
    match.starts_at = starts_at
    other_match.starts_at = starts_at
    match.save(update_fields=["starts_at"])
    other_match.save(update_fields=["starts_at"])
    client.force_login(manager)

    response = client.patch(
        f"/api/tournaments/{tournament.id_uuid}/matches/{match.id_uuid}/referee-duty/",
        data={"team_id": str(duty_team.id_uuid)},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == (
        f"{duty_team.name} speelt of fluit wedstrijd {other_match.match_number} "
        "al op dit tijdstip."
    )
    match.refresh_from_db()
    assert match.referee_team_id is None


def test_manager_cannot_assign_two_overlapping_duties_to_one_team(
    client: Client,
) -> None:
    """A team cannot be scheduled to referee two simultaneous fixtures."""
    manager, _, tournament, _, _, match, other_match = _match_graph()
    duty_team = match.tournament.teams.exclude(
        pk__in=(
            match.home_team_id,
            match.away_team_id,
            other_match.home_team_id,
            other_match.away_team_id,
        )
    ).first()
    if duty_team is None:
        duty_team = TournamentTeam.objects.create(
            tournament=tournament,
            name="Team 5",
        )
    starts_at = timezone.now()
    match.starts_at = starts_at
    other_match.starts_at = starts_at
    match.referee_team = duty_team
    match.save(update_fields=["starts_at", "referee_team"])
    other_match.save(update_fields=["starts_at"])
    client.force_login(manager)

    response = client.patch(
        f"/api/tournaments/{tournament.id_uuid}/matches/"
        f"{other_match.id_uuid}/referee-duty/",
        data={"team_id": str(duty_team.id_uuid)},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == (
        f"{duty_team.name} speelt of fluit wedstrijd {match.match_number} "
        "al op dit tijdstip."
    )
    other_match.refresh_from_db()
    assert other_match.referee_team_id is None


@override_settings(
    ALLOWED_HOSTS=["api.korfbal.butrosgroot.com", "testserver"],
    KORFBAL_ORIGIN="https://api.korfbal.butrosgroot.com",
    WEB_APP_ORIGIN="https://korfbal.localhost",
    WEB_KORFBAL_ORIGIN="https://korfbal.butrosgroot.com",
)
def test_referee_pdf_exports_unassigned_matches_with_direct_links(
    client: Client,
) -> None:
    """Unknown knockout referees do not block the printable match QR pack."""
    manager, _, tournament, _, _, _, _ = _match_graph()
    client.force_login(manager)

    with patch(
        "apps.tournament.api.views.build_referee_duties_pdf",
        return_value=b"%PDF-1.4\n%%EOF\n",
    ) as build_pdf:
        response = client.get(
            f"/api/tournaments/{tournament.id_uuid}/referee-duties.pdf",
            headers={"host": "api.korfbal.butrosgroot.com"},
        )

    assert response.status_code == HTTPStatus.OK
    _, duties = build_pdf.call_args.args
    assert [duty.referee_team_name for duty in duties] == [
        "Nog niet toegewezen",
        "Nog niet toegewezen",
    ]
    assert len({duty.access_url for duty in duties}) == EXPECTED_PDF_DUTIES
    assert all(
        duty.access_url.startswith(
            "https://korfbal.butrosgroot.com/tournaments/referee/"
        )
        for duty in duties
    )
    assert not tournament.matches.filter(referee_access_token=None).exists()


@override_settings(
    ALLOWED_HOSTS=["api.korfbal.butrosgroot.com", "testserver"],
    KORFBAL_ORIGIN="https://api.korfbal.butrosgroot.com",
    WEB_APP_ORIGIN="https://korfbal.localhost",
    WEB_KORFBAL_ORIGIN="https://korfbal.butrosgroot.com",
)
def test_manager_exports_printable_referee_duties(client: Client) -> None:
    """The PDF export receives one correctly labelled QR card per match."""
    manager, _, tournament, _, _, match_one, match_two = _match_graph()
    duty_team_one = tournament.teams.exclude(
        pk__in=(match_one.home_team_id, match_one.away_team_id)
    ).first()
    assert duty_team_one is not None
    duty_team_two = match_one.home_team
    assert duty_team_two is not None
    match_one.referee_team = duty_team_one
    match_one.save(update_fields=["referee_team"])
    match_two.referee_team = duty_team_two
    match_two.save(update_fields=["referee_team"])
    client.force_login(manager)

    with patch(
        "apps.tournament.api.views.build_referee_duties_pdf",
        return_value=b"%PDF-1.4\n%%EOF\n",
    ) as build_pdf:
        response = client.get(
            f"/api/tournaments/{tournament.id_uuid}/referee-duties.pdf",
            headers={"host": "api.korfbal.butrosgroot.com"},
        )

    assert response.status_code == HTTPStatus.OK
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store"
    assert response["Content-Disposition"] == (
        'attachment; filename="referee-cup-scheidsrechter-qr.pdf"'
    )
    assert response.content == b"%PDF-1.4\n%%EOF\n"
    tournament_name, duties = build_pdf.call_args.args
    assert tournament_name == "Referee Cup"
    assert [duty.match_number for duty in duties] == [1, 2]
    assert duties[0].referee_team_name == duty_team_one.name
    assert duties[0].home_team_name == "Team 1"
    assert duties[0].away_team_name == "Team 2"
    assert duties[0].field_label == "Field 1"
    assert duties[0].starts_at_label == "Tijd nog niet bekend"
    assert duties[0].access_url.startswith(
        "https://korfbal.butrosgroot.com/tournaments/referee/"
    )
    assert duties[0].access_url != duties[1].access_url
    match_one.refresh_from_db()
    match_two.refresh_from_db()
    assert match_one.referee_access_token is not None
    assert match_two.referee_access_token is not None


def test_direct_match_qr_claims_without_a_referee_team_and_expires(
    client: Client,
) -> None:
    """A match QR opens only its fixture and works before a team is known."""
    _, _, _, _, _, match, denied_match = _match_graph()
    match.referee_access_token = uuid4()
    match.save(update_fields=["referee_access_token"])
    duties_url = f"/api/tournaments/referee-duties/{match.referee_access_token}/"

    duties = client.get(duties_url)
    claim = client.post(
        f"{duties_url}matches/{match.id_uuid}/claim/",
        data={"name": "Sam de Vrij"},
        content_type="application/json",
    )

    assert duties.status_code == HTTPStatus.OK
    assert duties.json()["access_kind"] == "match"
    assert duties.json()["team"] is None
    assert [row["id_uuid"] for row in duties.json()["matches"]] == [str(match.id_uuid)]
    assert claim.status_code == HTTPStatus.OK
    claim_token = claim.json()["claim_token"]
    assert (
        client.get(
            f"/api/tournaments/matches/{match.id_uuid}/tracker/?token={claim_token}"
        ).status_code
        == HTTPStatus.OK
    )
    assert (
        client.post(
            f"{duties_url}matches/{denied_match.id_uuid}/claim/",
            data={"name": "Sam de Vrij"},
            content_type="application/json",
        ).status_code
        == HTTPStatus.NOT_FOUND
    )

    match.status = TournamentMatch.Status.FINAL
    match.save(update_fields=["status"])
    assert client.get(duties_url).status_code == HTTPStatus.NOT_FOUND
    assert (
        client.get(
            f"/api/tournaments/matches/{match.id_uuid}/tracker/?token={claim_token}"
        ).status_code
        == HTTPStatus.FORBIDDEN
    )


def test_guest_claim_scores_match_and_expires_when_final(client: Client) -> None:
    """A QR claim authorizes one match only until its final result is saved."""
    _, _, tournament, _, _, match, denied_match = _match_graph()
    duty_team = tournament.teams.exclude(
        pk__in=(match.home_team_id, match.away_team_id)
    ).first()
    assert duty_team is not None
    duty_team.referee_access_token = uuid4()
    duty_team.save(update_fields=["referee_access_token"])
    match.referee_team = duty_team
    match.save(update_fields=["referee_team"])

    duties_url = f"/api/tournaments/referee-duties/{duty_team.referee_access_token}/"
    duties = client.get(duties_url)
    claim = client.post(
        f"{duties_url}matches/{match.id_uuid}/claim/",
        data={"name": "Robin de Boer"},
        content_type="application/json",
    )

    assert duties.status_code == HTTPStatus.OK
    assert duties.json()["access_kind"] == "team"
    assert duties.json()["team"]["name"] == duty_team.name
    assert duties.json()["matches"][0]["can_claim"] is True
    assert claim.status_code == HTTPStatus.OK
    claim_token = claim.json()["claim_token"]

    tracker_url = (
        f"/api/tournaments/matches/{match.id_uuid}/tracker/?token={claim_token}"
    )
    assert client.get(tracker_url).status_code == HTTPStatus.OK
    assert (
        client.get(
            f"/api/tournaments/matches/{denied_match.id_uuid}/tracker/?token={claim_token}"
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    ready = client.post(
        f"/api/tournaments/matches/{match.id_uuid}/tracker/ready/?token={claim_token}",
        data={"expected_revision": 1},
        content_type="application/json",
    )
    goal = client.post(
        f"/api/tournaments/matches/{match.id_uuid}/tracker/goal/?token={claim_token}",
        data={"side": "home", "expected_revision": 2},
        content_type="application/json",
    )

    assert ready.status_code == HTTPStatus.OK
    assert goal.status_code == HTTPStatus.OK
    audit = TournamentResultAudit.objects.get(
        match=match,
        source=TournamentResultAudit.Source.REFEREE_GOAL,
    )
    assert audit.changed_by_id is None
    assert audit.changed_by_name == "Robin de Boer"
    match.refresh_from_db()
    assert match.field_ready_by_id is None
    assert match.field_ready_by_name == "Robin de Boer"

    removed = client.delete(
        f"/api/tournaments/matches/{match.id_uuid}/tracker/events/latest/"
        f"?token={claim_token}",
        data={
            "event_id": goal.json()["latest_event"]["id_uuid"],
            "expected_revision": 3,
        },
        content_type="application/json",
    )
    assert removed.status_code == HTTPStatus.OK
    assert removed.json()["match"]["home_score"] is None
    undo_audit = TournamentResultAudit.objects.get(
        match=match,
        source=TournamentResultAudit.Source.REFEREE_UNDO,
    )
    assert undo_audit.changed_by_id is None
    assert undo_audit.changed_by_name == "Robin de Boer"

    match.status = TournamentMatch.Status.FINAL
    match.save(update_fields=["status"])
    assert client.get(tracker_url).status_code == HTTPStatus.FORBIDDEN


def test_guest_can_claim_as_player_from_linked_team_roster(client: Client) -> None:
    """The team QR offers roster players active on the tournament date."""
    _, referee, tournament, _, _, match, _ = _match_graph()
    referee.first_name = "Noa"
    referee.last_name = "Jansen"
    referee.save(update_fields=["first_name", "last_name"])
    club = Club.objects.create(name="Roster Club")
    linked_team = Team.objects.create(name="B1", club=club)
    event_date = timezone.localdate(tournament.starts_at)
    season = Season.objects.create(
        name=f"Referee season {uuid4()}",
        start_date=event_date - timedelta(days=1),
        end_date=event_date + timedelta(days=1),
    )
    roster = TeamData.objects.create(team=linked_team, season=season)
    roster.players.add(referee.player)
    duty_team = tournament.teams.exclude(
        pk__in=(match.home_team_id, match.away_team_id)
    ).first()
    assert duty_team is not None
    duty_team.linked_team = linked_team
    duty_team.referee_access_token = uuid4()
    duty_team.save(update_fields=["linked_team", "referee_access_token"])
    match.referee_team = duty_team
    match.save(update_fields=["referee_team"])
    duties_url = f"/api/tournaments/referee-duties/{duty_team.referee_access_token}/"

    duties = client.get(duties_url)
    claim = client.post(
        f"{duties_url}matches/{match.id_uuid}/claim/",
        data={"player_id": str(referee.player.id_uuid)},
        content_type="application/json",
    )

    assert duties.status_code == HTTPStatus.OK
    assert duties.json()["players"] == [
        {"id_uuid": str(referee.player.id_uuid), "name": "Noa Jansen"}
    ]
    assert claim.status_code == HTTPStatus.OK
    match.refresh_from_db()
    assert match.referee_player_id == referee.player.id_uuid
    assert match.referee_name == "Noa Jansen"
