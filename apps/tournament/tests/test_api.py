"""End-to-end tournament API permission and scoring tests."""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
import pytest

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentResultAudit,
    TournamentStage,
    TournamentTeam,
)


pytestmark = pytest.mark.django_db
EXPECTED_ROUND_ROBIN_MATCHES = 6


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
