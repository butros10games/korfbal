# ruff: noqa: D103
"""First-run setup: spectators, player registration, club claims and their review."""

from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

from django.contrib.admin import site
from django.contrib.auth.models import Permission, User
from django.core import mail
from django.db import OperationalError
from django.test import RequestFactory, override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.composition import join_request_ports
from apps.club.models import Club, ClubAdmin, ClubJoinRequest
from apps.club.services.join_requests import JoinRequestError, decide_request
from apps.club.tasks import (
    link_join_request_identity,
    notify_join_request_decision,
    notify_join_request_reviewers,
    resume_stalled_join_request_links,
)
from apps.competition.models import SyncLease
from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.tasks import execute_job
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


# PostgreSQL rejects row locks on the nullable side of an outer join; the parity
# lane reruns these decisions there.
pytestmark = [pytest.mark.postgres_parity, pytest.mark.django_db]

PERSON_ID = "NFT12K3"
REQUESTS_PER_HOUR = 5
ONBOARDING = "/api/club/onboarding/"


@pytest.fixture
def season() -> Season:
    """Create the running season."""
    today = timezone.localdate()
    return Season.objects.create(
        name="Running",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=200),
    )


@pytest.fixture
def club() -> Club:
    """Create the club new players register with."""
    return Club.objects.create(name="KV Example")


@pytest.fixture
def team(club: Club, season: Season) -> Team:
    """Create a team that plays in the running season."""
    team = Team.objects.create(name="Example 1", club=club)
    TeamData.objects.create(team=team, season=season)
    return team


@pytest.fixture
def imported(team: Team, season: Season) -> Player:
    """Create a visible imported KNKV player on the team's roster."""
    player = Player.all_objects.create(
        name="Imported Player",
        knkv_person_id=PERSON_ID,
        knkv_privacy="NORMAL",
        knkv_observed_at=timezone.now(),
    )
    TeamData.objects.get(team=team, season=season).players.add(player)
    return player


def _client(username: str, **fields: object) -> tuple[Client, Player]:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.com", **fields
    )
    client = Client()
    client.force_login(user)
    return client, Player.all_objects.get(user=user)


def _admin_client(club: Club) -> Client:
    client, player = _client("club-admin")
    ClubAdmin.objects.create(club=club, player=player)
    return client


def _jobs(task: str) -> list[BackgroundJob]:
    return list(BackgroundJob.objects.filter(task=f"apps.club.tasks.{task}"))


def test_new_accounts_start_setup_and_can_finish_as_spectator() -> None:
    client, player = _client("spectator")

    assert client.get(ONBOARDING).json()["completed"] is False

    response = client.post(f"{ONBOARDING}spectator/")

    assert response.status_code == HTTPStatus.OK
    assert response.json() | {"requests": []} == {
        "role": "spectator",
        "completed": True,
        "knkv_linked": False,
        "requests": [],
    }
    player.refresh_from_db()
    assert player.onboarded_at is not None


def test_setup_endpoints_require_a_signed_in_account() -> None:
    response = Client().get(ONBOARDING)

    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}


def test_knkv_lookup_finds_only_visible_unclaimed_imports(
    imported: Player, club: Club, team: Team
) -> None:
    client, _ = _client("lookup")

    found = client.post(
        f"{ONBOARDING}knkv-lookup/", {"knkv_person_id": PERSON_ID.lower()}
    ).json()

    assert found["match"] == {
        "knkv_person_id": PERSON_ID,
        "name": "Imported Player",
        "club": {
            "id_uuid": str(club.pk),
            "name": club.name,
            "logo_url": club.get_club_logo(),
        },
        "team": {"id_uuid": str(team.pk), "name": team.name},
    }

    Player.all_objects.filter(pk=imported.pk).update(knkv_privacy="PRIVATE")
    hidden = client.post(f"{ONBOARDING}knkv-lookup/", {"knkv_person_id": PERSON_ID})
    assert hidden.json() == {"match": None}


def test_club_search_and_teams_list_running_season_teams(
    club: Club, team: Team
) -> None:
    client, _ = _client("search")
    Club.objects.create(name="KV Example Oud", dissolved=True)
    Team.objects.create(name="Retired", club=club)

    clubs = client.get(f"{ONBOARDING}clubs/", {"search": "example"}).json()
    teams = client.get(f"{ONBOARDING}clubs/{club.pk}/teams/").json()

    assert [row["name"] for row in clubs["results"]] == ["KV Example"]
    assert teams == {"results": [{"id_uuid": str(team.pk), "name": "Example 1"}]}


def test_player_request_with_knkv_identity_follows_the_club_and_notifies(
    imported: Player, club: Club, team: Team
) -> None:
    client, player = _client("knkv-player")

    response = client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "team_id": str(team.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED, response.json()
    body = response.json()
    assert body["role"] == "player"
    assert body["completed"] is True
    assert body["requests"][0]["status"] == "pending"
    assert body["requests"][0]["knkv_person_id"] == PERSON_ID
    assert list(player.club_follow.all()) == [club]
    assert list(player.team_follow.all()) == [team]
    assert len(_jobs("notify_join_request_reviewers")) == 1
    # Nothing is linked before a club admin confirms the claim.
    assert Player.all_objects.get(knkv_person_id=PERSON_ID).pk == imported.pk


def test_player_request_rejects_a_knkv_identity_from_another_club(
    imported: Player,
) -> None:
    client, _ = _client("wrong-club")
    other = Club.objects.create(name="Other Club")

    response = client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(other.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not ClubJoinRequest.objects.exists()


def test_one_pending_request_per_club(club: Club) -> None:
    client, _ = _client("twice")
    payload = {"club_id": str(club.pk)}

    first = client.post(
        f"{ONBOARDING}player/", payload, content_type="application/json"
    )
    second = client.post(
        f"{ONBOARDING}player/", payload, content_type="application/json"
    )

    assert first.status_code == HTTPStatus.CREATED
    assert second.status_code == HTTPStatus.BAD_REQUEST
    assert ClubJoinRequest.objects.count() == 1


def test_reviewers_receive_email_and_push_intent(club: Club, team: Team) -> None:
    _admin_client(club)
    client, _ = _client("private-player")
    client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "team_id": str(team.pk)},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()

    notify_join_request_reviewers(request_id=str(request.pk))

    assert mail.outbox[0].to == ["club-admin@example.com"]
    assert "private-player wil als speler lid worden" in mail.outbox[0].body


@override_settings(KORFBAL_SUPPORT_EMAIL="support@example.com")
def test_clubs_without_admin_notify_the_platform_team(club: Club) -> None:
    client, _ = _client("no-admin-yet")
    client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk)},
        content_type="application/json",
    )

    notify_join_request_reviewers(request_id=str(ClubJoinRequest.objects.get().pk))

    assert mail.outbox[0].to == ["support@example.com"]


def test_club_admin_approves_a_private_player_onto_the_team(
    club: Club, team: Team, season: Season
) -> None:
    admin = _admin_client(club)
    client, player = _client("private")
    client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "team_id": str(team.pk)},
        content_type="application/json",
    )
    listed = admin.get(f"/api/club/clubs/{club.pk}/join-requests/").json()["results"]

    response = admin.post(
        f"/api/club/clubs/{club.pk}/join-requests/{listed[0]['id_uuid']}/",
        {"decision": "approve"},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "approved"
    assert PlayerClubMembership.objects.filter(player=player, club=club).exists()
    roster = TeamData.objects.get(team=team, season=season)
    assert roster.players.filter(pk=player.pk).exists()
    assert len(_jobs("notify_join_request_decision")) == 1
    notify_join_request_decision(request_id=listed[0]["id_uuid"])
    assert mail.outbox[-1].subject == "[KorfConnect] Aanmelding geaccepteerd"


def test_approval_links_the_claimed_knkv_player_after_the_import_pauses(
    imported: Player, club: Club, team: Team, season: Season
) -> None:
    admin = _admin_client(club)
    client, player = _client("claimant")
    client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "team_id": str(team.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()
    listed = admin.get(f"/api/club/clubs/{club.pk}/join-requests/").json()["results"]
    assert listed[0]["knkv_name"] == "Imported Player"
    admin.post(
        f"/api/club/clubs/{club.pk}/join-requests/{request.pk}/",
        {"decision": "approve"},
        content_type="application/json",
    )
    assert len(_jobs("link_join_request_identity")) == 1

    SyncLease.objects.create(
        key="sportlink", expires_at=timezone.now() + timedelta(minutes=5)
    )
    # Run as the job envelope does: the job is claimed while the task executes.
    BackgroundJob.objects.filter(task__endswith="link_join_request_identity").update(
        attempts=1
    )
    link_join_request_identity(request_id=str(request.pk))
    request.refresh_from_db()
    assert request.link_status == "pending"
    retry = _jobs("link_join_request_identity")[0].next_due_at
    assert retry is not None
    assert retry > timezone.now()

    SyncLease.objects.filter(key="sportlink").update(expires_at=timezone.now())
    link_join_request_identity(request_id=str(request.pk))

    request.refresh_from_db()
    assert request.link_status == "linked"
    assert Player.all_objects.get(knkv_person_id=PERSON_ID).pk == player.pk
    assert not Player.all_objects.filter(pk=imported.pk).exists()
    roster = TeamData.objects.get(team=team, season=season)
    assert roster.players.filter(pk=player.pk).exists()


def test_only_club_admins_review_member_requests(club: Club) -> None:
    client, _ = _client("outsider")
    _client("applicant")[0].post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk)},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()

    listed = client.get(f"/api/club/clubs/{club.pk}/join-requests/")
    decided = client.post(
        f"/api/club/clubs/{club.pk}/join-requests/{request.pk}/",
        {"decision": "approve"},
        content_type="application/json",
    )

    assert listed.status_code == HTTPStatus.FORBIDDEN
    assert decided.status_code == HTTPStatus.FORBIDDEN


@override_settings(KORFBAL_SUPPORT_EMAIL="support@example.com")
def test_club_claims_go_to_platform_staff_not_club_admins(club: Club) -> None:
    admin = _admin_client(club)
    client, player = _client("secretary")

    response = client.post(
        f"{ONBOARDING}club/",
        {"club_id": str(club.pk), "note": "Secretaris"},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()
    notify_join_request_reviewers(request_id=str(request.pk))
    by_admin = admin.post(
        f"/api/club/clubs/{club.pk}/join-requests/{request.pk}/",
        {"decision": "approve"},
        content_type="application/json",
    )

    assert response.json()["role"] == "club"
    assert mail.outbox[0].to == ["support@example.com"]
    assert by_admin.status_code == HTTPStatus.CONFLICT
    decide_request(
        request_id=str(request.pk),
        club=None,
        approve=True,
        reviewer=None,
        ports=join_request_ports,
    )
    assert ClubAdmin.objects.filter(club=club, player=player).exists()
    with pytest.raises(JoinRequestError):
        decide_request(
            request_id=str(request.pk),
            club=None,
            approve=False,
            reviewer=None,
            ports=join_request_ports,
        )


def test_requesters_withdraw_only_their_own_pending_request(club: Club) -> None:
    client, _ = _client("withdrawer")
    other, _ = _client("other")
    client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk)},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()

    assert (
        other.delete(f"{ONBOARDING}requests/{request.pk}/").status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        client.delete(f"{ONBOARDING}requests/{request.pk}/").status_code
        == HTTPStatus.NO_CONTENT
    )
    request.refresh_from_db()
    assert request.status == "withdrawn"
    assert client.get(ONBOARDING).json()["requests"] == []


@pytest.mark.parametrize("with_team", [True, False])
def test_club_admins_reject_requests_with_and_without_a_team(
    club: Club, team: Team, *, with_team: bool
) -> None:
    admin = _admin_client(club)
    payload = {"club_id": str(club.pk)} | (
        {"team_id": str(team.pk)} if with_team else {}
    )
    _client("rejected")[0].post(
        f"{ONBOARDING}player/", payload, content_type="application/json"
    )
    request = ClubJoinRequest.objects.get()

    response = admin.post(
        f"/api/club/clubs/{club.pk}/join-requests/{request.pk}/",
        {"decision": "reject"},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    request.refresh_from_db()
    assert request.status == "rejected"
    assert not PlayerClubMembership.objects.exists()


def test_player_requests_share_the_relation_number_lookup_limit(
    imported: Player, club: Club
) -> None:
    client, _ = _client("guesser")
    for _ in range(10):
        client.post(f"{ONBOARDING}knkv-lookup/", {"knkv_person_id": "ABC1234"})

    response = client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert not ClubJoinRequest.objects.exists()


def test_repeated_submit_and_withdraw_is_rate_limited(club: Club) -> None:
    client, _ = _client("spammer")
    statuses = []
    for _ in range(6):
        response = client.post(
            f"{ONBOARDING}player/",
            {"club_id": str(club.pk)},
            content_type="application/json",
        )
        statuses.append(response.status_code)
        for request in ClubJoinRequest.objects.filter(status="pending"):
            client.delete(f"{ONBOARDING}requests/{request.pk}/")

    assert statuses == [HTTPStatus.CREATED] * REQUESTS_PER_HOUR + [
        HTTPStatus.TOO_MANY_REQUESTS
    ]
    assert len(_jobs("notify_join_request_reviewers")) == REQUESTS_PER_HOUR


def test_linking_fails_visibly_when_the_import_disappeared(
    imported: Player, club: Club
) -> None:
    admin = _admin_client(club)
    _client("late-link")[0].post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()
    admin.post(
        f"/api/club/clubs/{club.pk}/join-requests/{request.pk}/",
        {"decision": "approve"},
        content_type="application/json",
    )
    # The provider no longer reports this relation number.
    Player.all_objects.filter(pk=imported.pk).update(knkv_person_id="RENUMBERED")

    link_join_request_identity(request_id=str(request.pk))

    request.refresh_from_db()
    assert request.link_status == "failed"


def test_unexpected_link_errors_retry_until_the_deadline_then_fail(
    imported: Player, club: Club
) -> None:
    admin = _admin_client(club)
    _client("storage-error")[0].post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()
    admin.post(
        f"/api/club/clubs/{club.pk}/join-requests/{request.pk}/",
        {"decision": "approve"},
        content_type="application/json",
    )
    BackgroundJob.objects.filter(task__endswith="link_join_request_identity").update(
        attempts=1
    )

    with patch("apps.club.tasks.link_players", side_effect=FileNotFoundError):
        link_join_request_identity(request_id=str(request.pk))
        request.refresh_from_db()
        assert request.link_status == "pending"
        assert _jobs("link_join_request_identity")[0].next_due_at is not None

        ClubJoinRequest.objects.filter(pk=request.pk).update(
            decided_at=timezone.now() - timedelta(days=3)
        )
        link_join_request_identity(request_id=str(request.pk))

    request.refresh_from_db()
    assert request.link_status == "failed"
    assert request.link_error == "Koppelen mislukt (FileNotFoundError)."


def _approved_claim(club: Club, username: str) -> ClubJoinRequest:
    admin = _admin_client(club)
    _client(username)[0].post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )
    request = ClubJoinRequest.objects.get()
    admin.post(
        f"/api/club/clubs/{club.pk}/join-requests/{request.pk}/",
        {"decision": "approve"},
        content_type="application/json",
    )
    return request


def test_database_errors_in_the_job_envelope_keep_linking_scheduled(
    imported: Player, club: Club
) -> None:
    request = _approved_claim(club, "db-error")
    job = _jobs("link_join_request_identity")[0]

    with patch("apps.club.tasks._importer_running", side_effect=OperationalError):
        for _ in range(8):
            BackgroundJob.objects.filter(pk=job.pk).update(
                due_at=timezone.now() - timedelta(seconds=1), next_due_at=None
            )
            execute_job.run(job.pk)

    job.refresh_from_db()
    request.refresh_from_db()
    assert request.link_status == "pending"
    assert job.due_at is not None


def test_sweep_requeues_links_whose_job_stopped(imported: Player, club: Club) -> None:
    request = _approved_claim(club, "stalled")
    BackgroundJob.objects.filter(task__endswith="link_join_request_identity").update(
        due_at=None, next_due_at=None
    )

    resume_stalled_join_request_links()

    assert _jobs("link_join_request_identity")[0].due_at is not None
    ClubJoinRequest.objects.filter(pk=request.pk).update(
        decided_at=timezone.now() - timedelta(days=3)
    )
    BackgroundJob.objects.filter(task__endswith="link_join_request_identity").update(
        due_at=None, next_due_at=None
    )
    resume_stalled_join_request_links()
    request.refresh_from_db()
    assert request.link_status == "failed"


def test_reviewers_do_not_see_names_of_imports_that_became_hidden(
    imported: Player, club: Club
) -> None:
    admin = _admin_client(club)
    _client("hidden-claim")[0].post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "knkv_person_id": PERSON_ID},
        content_type="application/json",
    )
    Player.all_objects.filter(pk=imported.pk).update(
        knkv_observed_at=timezone.now() - timedelta(days=9)
    )

    listed = admin.get(f"/api/club/clubs/{club.pk}/join-requests/").json()["results"]

    assert listed[0]["knkv_person_id"] == PERSON_ID
    assert listed[0]["knkv_name"] is None


def test_claims_of_imports_without_a_club_are_refused(club: Club) -> None:
    Player.all_objects.create(
        name="Clubless",
        knkv_person_id="NOCLUB1",
        knkv_privacy="NORMAL",
        knkv_observed_at=timezone.now(),
    )
    client, _ = _client("clubless")

    response = client.post(
        f"{ONBOARDING}player/",
        {"club_id": str(club.pk), "knkv_person_id": "NOCLUB1"},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert not ClubJoinRequest.objects.exists()


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        (("view_clubjoinrequest",), set()),
        (("view_clubjoinrequest", "change_clubjoinrequest"), {"approve", "reject"}),
    ],
)
def test_only_staff_who_may_change_requests_can_decide_them(
    permissions: tuple[str, ...], expected: set[str]
) -> None:
    staff = User.objects.create_user(username="reviewer", is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="club", codename__in=permissions
        )
    )
    request = RequestFactory().get("/admin/club/clubjoinrequest/")
    request.user = User.objects.get(pk=staff.pk)

    actions = site._registry[ClubJoinRequest].get_actions(request)

    assert set(actions) & {"approve", "reject"} == expected
