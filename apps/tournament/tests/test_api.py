"""End-to-end tournament API permission and scoring tests."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
import pytest

from apps.tournament.composition import change_publisher
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentResultAudit,
    TournamentStage,
    TournamentTeam,
)


pytestmark = pytest.mark.django_db
OnCommitCapture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
EXPECTED_ROUND_ROBIN_MATCHES = 6
EXPECTED_SEMIFINALS = 2
FINAL_MATCH_REVISION = 3
REOPENED_MATCH_REVISION = 4


def _create_tournament(client: Client) -> dict[str, object]:
    response = client.post(
        "/api/tournaments/",
        data={
            "name": "KWT Zomertoernooi",
            "location": "Sporthal De Korf",
            "starts_at": timezone.now().isoformat(),
            "win_points": 2,
            "draw_points": 1,
            "loss_points": 0,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CREATED
    return response.json()


def _create_ready_round() -> tuple[
    object,
    Tournament,
    TournamentStage,
    list[TournamentMatch],
]:
    user = get_user_model().objects.create_user(username="round-manager")
    tournament = Tournament.objects.create(
        name="Gelijktijdige ronde",
        slug="gelijktijdige-ronde",
        owner=user,
        starts_at=timezone.now(),
        status=Tournament.Status.PUBLISHED,
    )
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Poulefase",
        kind=TournamentStage.Kind.POOL,
    )
    fields = [
        TournamentField.objects.create(
            tournament=tournament,
            label=f"Veld {index}",
            sort_order=index,
        )
        for index in range(1, 3)
    ]
    teams = [
        TournamentTeam.objects.create(
            tournament=tournament,
            name=f"Rondeteam {index}",
            seed=index,
        )
        for index in range(1, 5)
    ]
    ready_at = timezone.now()
    matches = [
        TournamentMatch.objects.create(
            tournament=tournament,
            stage=stage,
            field=fields[index],
            home_team=teams[index * 2],
            away_team=teams[index * 2 + 1],
            round_number=2,
            match_number=index + 1,
            field_ready_at=ready_at,
            field_ready_by=user,
            field_ready_by_name=str(user),
        )
        for index in range(2)
    ]
    TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        field=fields[0],
        home_team=teams[0],
        away_team=teams[2],
        round_number=3,
        match_number=3,
        field_ready_at=ready_at,
    )
    return user, tournament, stage, matches


def test_anonymous_create_returns_json_auth_error(client: Client) -> None:
    """Management APIs never redirect anonymous clients to an HTML login page."""
    response = client.post(
        "/api/tournaments/",
        data={"name": "Verboden"},
        content_type="application/json",
    )

    assert response.status_code in {401, 403}
    assert response.headers["Content-Type"].startswith("application/json")
    assert "Location" not in response.headers


def test_new_tournament_is_saved_as_a_manager_only_draft(client: Client) -> None:
    """An organizer can leave after creation and resume the persisted draft later."""
    user = get_user_model().objects.create_user(username="draft-manager")
    client.force_login(user)

    created = client.post(
        "/api/tournaments/",
        data={
            "name": "Concepttoernooi",
            "location": "Sporthal",
            "starts_at": timezone.now().isoformat(),
            "status": Tournament.Status.PUBLISHED,
        },
        content_type="application/json",
    )

    assert created.status_code == HTTPStatus.CREATED
    assert created.json()["status"] == Tournament.Status.DRAFT
    tournament_id = created.json()["id_uuid"]
    assert client.get("/api/tournaments/").json()[0]["id_uuid"] == tournament_id

    client.logout()
    assert client.get("/api/tournaments/").json() == []


def test_manager_generates_publishes_and_scores_live_tournament(client: Client) -> None:
    """A complete pool workflow publishes, scores, audits, and rejects stale writes."""
    user = get_user_model().objects.create_user(
        username="manager", password="test-pass"
    )
    client.force_login(user)
    tournament = _create_tournament(client)
    tournament_id = tournament["id_uuid"]

    for index in range(1, 5):
        response = client.post(
            f"/api/tournaments/{tournament_id}/teams/",
            data={"name": f"Team {index}", "seed": index},
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.CREATED
    for index in range(1, 3):
        response = client.post(
            f"/api/tournaments/{tournament_id}/fields/",
            data={"label": f"Veld {index}", "sort_order": index},
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.CREATED

    generation = {
        "pool_count": 1,
        "strategy": "snake",
        "legs": 1,
        "duration_minutes": 15,
        "changeover_minutes": 5,
        "minimum_rest_minutes": 5,
    }
    preview = client.post(
        f"/api/tournaments/{tournament_id}/generation/preview/",
        data=generation,
        content_type="application/json",
    )
    assert preview.status_code == HTTPStatus.OK
    assert len(preview.json()["matches"]) == EXPECTED_ROUND_ROBIN_MATCHES
    applied = client.post(
        f"/api/tournaments/{tournament_id}/generation/apply/",
        data=generation,
        content_type="application/json",
    )
    assert applied.status_code == HTTPStatus.OK
    match = applied.json()["matches"][0]

    published = client.post(f"/api/tournaments/{tournament_id}/publish/")
    assert published.status_code == HTTPStatus.OK
    slug = published.json()["slug"]
    client.logout()
    public = client.get(f"/api/tournaments/public/{slug}/")
    assert public.status_code == HTTPStatus.OK

    client.force_login(user)
    result_url = f"/api/tournaments/matches/{match['id_uuid']}/result/"
    result = client.patch(
        result_url,
        data={
            "home_score": 7,
            "away_score": 5,
            "status": "final",
            "expected_revision": 0,
        },
        content_type="application/json",
    )
    assert result.status_code == HTTPStatus.OK
    assert result.json()["revision"] == 1
    assert TournamentResultAudit.objects.count() == 1
    assert TournamentMatch.objects.get(pk=match["id_uuid"]).winner_id is not None

    stale = client.patch(
        result_url,
        data={
            "home_score": 8,
            "away_score": 5,
            "status": "final",
            "expected_revision": 0,
            "reason": "Correctie",
        },
        content_type="application/json",
    )
    assert stale.status_code == HTTPStatus.CONFLICT
    assert TournamentResultAudit.objects.count() == 1

    refreshed = client.get(f"/api/tournaments/public/{slug}/").json()
    standings = refreshed["pools"][0]["standings"]
    assert standings[0]["played"] == 1
    assert refreshed["tournament"]["live_revision"] > 0


def test_manager_starts_every_ready_match_in_a_round_at_one_instant(
    client: Client,
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """One central command starts the whole round and creates an audit per match."""
    user, tournament, stage, matches = _create_ready_round()
    client.force_login(user)

    with (
        patch.object(change_publisher, "publish") as publish,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = client.post(
            f"/api/tournaments/{tournament.id_uuid}/stages/"
            f"{stage.id_uuid}/rounds/2/start/"
        )

    assert response.status_code == HTTPStatus.OK
    for match in matches:
        match.refresh_from_db()
        assert match.status == TournamentMatch.Status.LIVE
        assert (match.home_score, match.away_score) == (0, 0)
        assert match.revision == 1
    assert matches[0].updated_at == matches[1].updated_at
    assert TournamentResultAudit.objects.filter(
        match__in=matches,
        previous_status=TournamentMatch.Status.SCHEDULED,
        new_status=TournamentMatch.Status.LIVE,
    ).count() == len(matches)
    assert (
        TournamentMatch.objects.get(match_number=3).status
        == TournamentMatch.Status.SCHEDULED
    )
    tournament.refresh_from_db()
    assert tournament.status == Tournament.Status.LIVE
    assert tournament.live_revision == 1
    publish.assert_called_once_with(
        tournament_id=str(tournament.id_uuid),
        revision=1,
    )


def test_manager_can_mark_one_scheduled_match_ready(client: Client) -> None:
    """The score-card quick action records readiness with its rendered revision."""
    user, tournament, _stage, matches = _create_ready_round()
    match = matches[0]
    match.field_ready_at = None
    match.field_ready_by = None
    match.field_ready_by_name = ""
    match.revision = FINAL_MATCH_REVISION
    match.save()
    client.force_login(user)

    response = client.post(
        f"/api/tournaments/matches/{match.id_uuid}/readiness/",
        data={"expected_revision": FINAL_MATCH_REVISION},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["revision"] == REOPENED_MATCH_REVISION
    match.refresh_from_db()
    assert match.field_ready_at is not None
    assert match.field_ready_by == user
    assert match.field_ready_by_name == str(user)
    tournament.refresh_from_db()
    assert tournament.live_revision == 1


def test_manager_reopens_final_match_with_score_and_audit_intact(
    client: Client,
) -> None:
    """Resetting definitive returns a match to live without losing its score."""
    user, _tournament, _stage, matches = _create_ready_round()
    match = matches[0]
    match.status = TournamentMatch.Status.FINAL
    match.home_score = 7
    match.away_score = 5
    match.winner = match.home_team
    match.revision = FINAL_MATCH_REVISION
    match.save()
    client.force_login(user)

    response = client.patch(
        f"/api/tournaments/matches/{match.id_uuid}/result/",
        data={
            "home_score": 7,
            "away_score": 5,
            "status": TournamentMatch.Status.LIVE,
            "expected_revision": FINAL_MATCH_REVISION,
            "reason": "Definitieve uitslag teruggezet door toernooibeheer",
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    match.refresh_from_db()
    assert match.status == TournamentMatch.Status.LIVE
    assert (match.home_score, match.away_score) == (7, 5)
    assert match.winner is None
    assert match.revision == REOPENED_MATCH_REVISION
    audit = TournamentResultAudit.objects.get(match=match)
    assert audit.previous_status == TournamentMatch.Status.FINAL
    assert audit.new_status == TournamentMatch.Status.LIVE
    assert audit.reason == "Definitieve uitslag teruggezet door toernooibeheer"


@pytest.mark.parametrize(
    ("initial_status", "expected_status", "expected_scores", "creates_audit"),
    [
        (
            TournamentMatch.Status.SCHEDULED,
            TournamentMatch.Status.SCHEDULED,
            (None, None),
            False,
        ),
        (
            TournamentMatch.Status.LIVE,
            TournamentMatch.Status.SCHEDULED,
            (None, None),
            True,
        ),
        (
            TournamentMatch.Status.FINAL,
            TournamentMatch.Status.LIVE,
            (7, 5),
            True,
        ),
        (
            TournamentMatch.Status.CANCELLED,
            TournamentMatch.Status.SCHEDULED,
            (None, None),
            True,
        ),
    ],
)
def test_manager_can_reset_every_match_state(
    client: Client,
    initial_status: str,
    expected_status: str,
    expected_scores: tuple[int | None, int | None],
    creates_audit: bool,
) -> None:
    """Every visible lifecycle state has a revision-safe recovery path."""
    user, tournament, _stage, matches = _create_ready_round()
    match = matches[0]
    match.status = initial_status
    match.home_score = 7 if initial_status != TournamentMatch.Status.SCHEDULED else None
    match.away_score = 5 if initial_status != TournamentMatch.Status.SCHEDULED else None
    match.winner = (
        match.home_team if initial_status == TournamentMatch.Status.FINAL else None
    )
    match.revision = FINAL_MATCH_REVISION
    match.save()
    client.force_login(user)

    response = client.post(
        f"/api/tournaments/matches/{match.id_uuid}/state/reset/",
        data={"expected_revision": FINAL_MATCH_REVISION},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["revision"] == REOPENED_MATCH_REVISION
    match.refresh_from_db()
    assert match.status == expected_status
    assert (match.home_score, match.away_score) == expected_scores
    assert match.winner is None
    if initial_status == TournamentMatch.Status.SCHEDULED:
        assert match.field_ready_at is None
    else:
        assert match.field_ready_at is not None
    audits = TournamentResultAudit.objects.filter(match=match)
    assert audits.exists() is creates_audit
    if creates_audit:
        audit = audits.get()
        assert audit.previous_status == initial_status
        assert audit.new_status == expected_status
        assert audit.reason == "Match state reset by tournament manager"
    tournament.refresh_from_db()
    assert tournament.live_revision == 1


def test_round_start_waits_until_every_scheduled_field_is_ready(client: Client) -> None:
    """A partial readiness state never starts only part of a round."""
    user, tournament, stage, matches = _create_ready_round()
    matches[1].field_ready_at = None
    matches[1].save(update_fields=["field_ready_at"])
    client.force_login(user)

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/stages/{stage.id_uuid}/rounds/2/start/"
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "niet alle velden" in response.json()["detail"]
    assert set(
        TournamentMatch.objects.filter(
            pk__in=[match.pk for match in matches]
        ).values_list("status", flat=True)
    ) == {TournamentMatch.Status.SCHEDULED}
    assert TournamentResultAudit.objects.filter(match__in=matches).count() == 0


def test_manager_can_reset_an_incorrect_readiness_signal(client: Client) -> None:
    """Readiness can be revoked before kickoff without touching the score."""
    user, tournament, _, matches = _create_ready_round()
    match = matches[0]
    client.force_login(user)
    readiness_url = f"/api/tournaments/matches/{match.id_uuid}/readiness/"

    response = client.delete(
        readiness_url,
        data={"expected_revision": match.revision},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["revision"] == 1
    match.refresh_from_db()
    assert match.field_ready_at is None
    assert match.field_ready_by_id is None
    assert not match.field_ready_by_name
    assert match.status == TournamentMatch.Status.SCHEDULED
    assert match.home_score is None
    assert match.away_score is None
    tournament.refresh_from_db()
    assert tournament.live_revision == 1


def test_manager_cannot_reset_readiness_after_kickoff(client: Client) -> None:
    """A live match keeps the readiness history that preceded its start."""
    user, _, _, matches = _create_ready_round()
    match = matches[0]
    match.status = TournamentMatch.Status.LIVE
    match.save(update_fields=["status"])
    client.force_login(user)

    response = client.delete(
        f"/api/tournaments/matches/{match.id_uuid}/readiness/",
        data={"expected_revision": match.revision},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    match.refresh_from_db()
    assert match.field_ready_at is not None


def test_unlisted_display_requires_token_and_manager_snapshot_exposes_it(
    client: Client,
) -> None:
    """Unlisted public displays use a receiver-safe token managers can copy."""
    user = get_user_model().objects.create_user(username="private-manager")
    client.force_login(user)
    tournament = _create_tournament(client)
    tournament_id = tournament["id_uuid"]
    team = client.post(
        f"/api/tournaments/{tournament_id}/teams/",
        data={"name": "Invitatie Team"},
        content_type="application/json",
    )
    assert team.status_code == HTTPStatus.CREATED
    record = Tournament.objects.get(pk=tournament_id)
    record.status = Tournament.Status.PUBLISHED
    record.visibility = Tournament.Visibility.UNLISTED
    record.save(update_fields=["status", "visibility"])

    snapshot = client.get(f"/api/tournaments/{tournament_id}/snapshot/")
    assert snapshot.status_code == HTTPStatus.OK
    token = snapshot.json()["capabilities"]["display_token"]
    assert snapshot.json()["teams"][0]["name"] == "Invitatie Team"

    client.logout()
    denied = client.get(f"/api/tournaments/public/{record.slug}/")
    allowed = client.get(f"/api/tournaments/public/{record.slug}/?token={token}")
    assert denied.status_code == HTTPStatus.FORBIDDEN
    assert allowed.status_code == HTTPStatus.OK


def test_pool_winners_generate_single_elimination_final(client: Client) -> None:
    """Finalized pools can feed an automatically wired knockout bracket."""
    user = get_user_model().objects.create_user(username="finals-manager")
    client.force_login(user)
    tournament_id = _create_tournament(client)["id_uuid"]
    for index in range(4):
        client.post(
            f"/api/tournaments/{tournament_id}/teams/",
            data={"name": f"Finalist {index + 1}", "seed": index + 1},
            content_type="application/json",
        )
    client.post(
        f"/api/tournaments/{tournament_id}/fields/",
        data={"label": "Finaleveld"},
        content_type="application/json",
    )
    applied = client.post(
        f"/api/tournaments/{tournament_id}/generation/apply/",
        data={"pool_count": 2, "strategy": "snake", "legs": 1},
        content_type="application/json",
    )
    assert applied.status_code == HTTPStatus.OK
    for match in applied.json()["matches"]:
        scored = client.patch(
            f"/api/tournaments/matches/{match['id_uuid']}/result/",
            data={
                "home_score": 6,
                "away_score": 4,
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert scored.status_code == HTTPStatus.OK

    generated = client.post(
        f"/api/tournaments/{tournament_id}/finals/generate/",
        data={"qualifiers_per_pool": 1},
        content_type="application/json",
    )
    assert generated.status_code == HTTPStatus.OK
    final_matches = [
        match
        for match in generated.json()["matches"]
        if match["stage_kind"] == TournamentStage.Kind.FINAL
    ]
    assert len(final_matches) == 1
    assert final_matches[0]["home_team"] is not None
    assert final_matches[0]["away_team"] is not None


def test_knockout_can_be_planned_early_and_fills_as_pools_finish(
    client: Client,
) -> None:
    """A general knockout keeps qualifier sources until results secure each slot."""
    user = get_user_model().objects.create_user(username="early-finals-manager")
    client.force_login(user)
    tournament_id = _create_tournament(client)["id_uuid"]
    for index in range(8):
        client.post(
            f"/api/tournaments/{tournament_id}/teams/",
            data={"name": f"Early finalist {index + 1}", "seed": index + 1},
            content_type="application/json",
        )
    client.post(
        f"/api/tournaments/{tournament_id}/fields/",
        data={"label": "Finaleveld"},
        content_type="application/json",
    )
    applied = client.post(
        f"/api/tournaments/{tournament_id}/generation/apply/",
        data={"pool_count": 4, "strategy": "snake", "legs": 1},
        content_type="application/json",
    )
    assert applied.status_code == HTTPStatus.OK

    planned = client.post(
        f"/api/tournaments/{tournament_id}/finals/generate/",
        data={"qualifiers_per_pool": 1},
        content_type="application/json",
    )
    assert planned.status_code == HTTPStatus.OK
    semifinals = [
        match
        for match in planned.json()["matches"]
        if match["stage_kind"] == TournamentStage.Kind.KNOCKOUT
    ]
    assert len(semifinals) == EXPECTED_SEMIFINALS
    assert all(
        match["home_team"] is None and match["away_team"] is None
        for match in semifinals
    )
    assert all(
        match["home_source_label"] is not None
        and match["away_source_label"] is not None
        for match in semifinals
    )

    pool_matches = [
        match for match in planned.json()["matches"] if match["pool_id"] is not None
    ]
    first_result = client.patch(
        f"/api/tournaments/matches/{pool_matches[0]['id_uuid']}/result/",
        data={
            "home_score": 4,
            "away_score": 2,
            "status": "final",
            "expected_revision": 0,
        },
        content_type="application/json",
    )
    assert first_result.status_code == HTTPStatus.OK
    partly_filled = client.get(f"/api/tournaments/{tournament_id}/snapshot/").json()
    semifinals = [
        match
        for match in partly_filled["matches"]
        if match["stage_kind"] == TournamentStage.Kind.KNOCKOUT
    ]
    assert (
        sum(
            team is not None
            for semifinal in semifinals
            for team in (semifinal["home_team"], semifinal["away_team"])
        )
        == 1
    )

    for pool_match in pool_matches[1:]:
        result = client.patch(
            f"/api/tournaments/matches/{pool_match['id_uuid']}/result/",
            data={
                "home_score": 5,
                "away_score": 1,
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert result.status_code == HTTPStatus.OK
    filled = client.get(f"/api/tournaments/{tournament_id}/snapshot/").json()
    semifinals = [
        match
        for match in filled["matches"]
        if match["stage_kind"] == TournamentStage.Kind.KNOCKOUT
    ]
    assert all(
        match["home_team"] is not None and match["away_team"] is not None
        for match in semifinals
    )


def test_scorekeeper_is_restricted_to_assigned_field(client: Client) -> None:
    """A scorekeeper may change only matches on their assigned field."""
    manager = get_user_model().objects.create_user(username="role-manager")
    scorekeeper = get_user_model().objects.create_user(username="scorekeeper")
    tournament = Tournament.objects.create(
        name="Rollen",
        slug="rollen",
        owner=manager,
        starts_at=timezone.now(),
    )
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Poules",
        kind=TournamentStage.Kind.POOL,
    )
    field_one = TournamentField.objects.create(tournament=tournament, label="Veld 1")
    field_two = TournamentField.objects.create(tournament=tournament, label="Veld 2")
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=f"Team {index}")
        for index in range(1, 5)
    ]
    matches = [
        TournamentMatch.objects.create(
            tournament=tournament,
            stage=stage,
            field=field,
            home_team=teams[index * 2],
            away_team=teams[index * 2 + 1],
            match_number=index + 1,
        )
        for index, field in enumerate((field_one, field_two))
    ]
    client.force_login(manager)
    member = client.post(
        f"/api/tournaments/{tournament.id_uuid}/members/",
        data={
            "user": scorekeeper.pk,
            "role": "scorekeeper",
            "field": str(field_one.id_uuid),
        },
        content_type="application/json",
    )
    assert member.status_code == HTTPStatus.CREATED

    client.force_login(scorekeeper)
    result = {
        "home_score": 3,
        "away_score": 2,
        "status": "final",
        "expected_revision": 0,
    }
    allowed = client.patch(
        f"/api/tournaments/matches/{matches[0].id_uuid}/result/",
        data=result,
        content_type="application/json",
    )
    denied = client.patch(
        f"/api/tournaments/matches/{matches[1].id_uuid}/result/",
        data=result,
        content_type="application/json",
    )
    assert allowed.status_code == HTTPStatus.OK
    assert denied.status_code == HTTPStatus.FORBIDDEN
