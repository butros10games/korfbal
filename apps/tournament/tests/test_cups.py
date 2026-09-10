"""Cup progression, bracket integrity and optimistic referee writes."""

from copy import deepcopy
from http import HTTPStatus
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentResultAudit,
    TournamentTeam,
)
from apps.tournament.services.cup_draw import generate_cup_draw
from apps.tournament.services.cups import CupError, cup_state, shootout_winner


pytestmark = pytest.mark.django_db
INITIAL_SCORE = 10
TWO_ATTEMPTS = 2
RULES = {"regular_minutes": [30, 30], "extra_minutes": [5, 5], "shootout_attempts": 1}


def assert_metadata_only_audit_reads(queries: CaptureQueriesContext) -> None:
    """Goal lookup must not fetch complete shootout history to inspect metadata."""
    reads = [
        query["sql"]
        for query in queries
        if query["sql"].startswith("SELECT")
        and f'FROM "{TournamentResultAudit._meta.db_table}"' in query["sql"]
    ]
    assert reads
    for sql in reads:
        assert "previous_cup_state" not in sql
        assert "new_cup_state" not in sql


@pytest.fixture
def cup(client: Client) -> Tournament:
    """Provide an owned, four-team cup with no provider-derived defaults."""
    owner = get_user_model().objects.create_user(username="cup-owner")
    client.force_login(owner)
    tournament = Tournament.objects.create(
        name="Synthetic Cup",
        slug="synthetic-cup",
        owner=owner,
        starts_at=timezone.now(),
        cup_rules=deepcopy(RULES),
    )
    TournamentField.objects.create(tournament=tournament, label="Veld 1")
    for seed in range(1, 5):
        TournamentTeam.objects.create(
            tournament=tournament, name=f"Team {seed}", seed=seed
        )
    generate_cup_draw(tournament)
    return tournament


def live_match(cup: Tournament) -> TournamentMatch:
    """Start a known first-round fixture."""
    match = cup.matches.order_by("match_number").first()
    assert match is not None
    match.status = TournamentMatch.Status.LIVE
    match.field_ready_at = timezone.now()
    match.home_score, match.away_score = 10, 10
    match.save()
    return match


def command(
    client: Client, match: TournamentMatch, action: str, **values: object
) -> HttpResponse:
    """Send a command using the last observed revision."""
    response = client.post(
        f"/api/tournaments/matches/{match.pk}/tracker/cup/",
        {"command": action, "expected_revision": match.revision, **values},
        content_type="application/json",
    )
    match.refresh_from_db()
    return response


def test_cup_plays_extra_time_and_separate_shootout(
    client: Client, cup: Tournament
) -> None:
    """The winner advances while penalties never inflate match goals."""
    match = live_match(cup)
    for expected in [("regular", 1), ("extra", 0), ("extra", 1), ("shootout", 0)]:
        response = command(client, match, "advance")
        assert response.status_code == HTTPStatus.OK, response.content
        assert (match.cup_state["phase"], match.cup_state["period"]) == expected
    assert command(client, match, "advance").status_code == HTTPStatus.CONFLICT
    assert (
        command(client, match, "penalty", side="away", scored=True).status_code
        == HTTPStatus.OK
    )
    assert (
        command(client, match, "penalty", side="away", scored=False).status_code
        == HTTPStatus.CONFLICT
    )
    assert (
        command(client, match, "penalty", side="home", scored=False).status_code
        == HTTPStatus.OK
    )
    assert command(client, match, "undo_penalty").status_code == HTTPStatus.OK
    assert (
        command(client, match, "penalty", side="home", scored=False).status_code
        == HTTPStatus.OK
    )
    assert command(client, match, "advance").status_code == HTTPStatus.OK
    assert match.status == TournamentMatch.Status.FINAL
    assert match.winner_id == match.away_team_id
    assert (match.home_score, match.away_score) == (10, 10)
    assert match.next_match.home_team_id == match.away_team_id
    audit = match.result_audits.filter(reason="Cup undo_penalty").get()
    assert len(audit.previous_cup_state["attempts"]) == TWO_ATTEMPTS
    assert len(audit.new_cup_state["attempts"]) == 1


def test_regulation_win_does_not_invent_extra_time(
    client: Client, cup: Tournament
) -> None:
    """Configured extra periods remain unplayed when regulation decides the match."""
    match = live_match(cup)
    match.home_score = 11
    match.save()
    command(client, match, "advance")
    command(client, match, "advance")
    assert match.status == TournamentMatch.Status.FINAL
    assert {p["phase"] for p in match.cup_state["completed_periods"]} == {"regular"}
    assert match.cup_state["attempts"] == []


def test_stale_unauthorized_and_score_bypass_rejected(
    client: Client, cup: Tournament
) -> None:
    """Cup writes share revisions and cannot be bypassed by direct result updates."""
    match = live_match(cup)
    assert (
        command(client, match, "advance", expected_revision=999).status_code
        == HTTPStatus.CONFLICT
    )
    assert not match.cup_state
    response = client.patch(
        f"/api/tournaments/matches/{match.pk}/result/",
        {
            "expected_revision": match.revision,
            "status": "final",
            "home_score": 11,
            "away_score": 10,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    client.logout()
    assert command(client, match, "advance").status_code in {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
    }


def test_shootout_rejects_normal_goals(client: Client, cup: Tournament) -> None:
    """A regular goal endpoint cannot write a penalty into the official score."""
    match = live_match(cup)
    for _ in range(4):
        command(client, match, "advance")
    response = client.post(
        f"/api/tournaments/matches/{match.pk}/tracker/goal/",
        {"side": "home", "expected_revision": match.revision},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CONFLICT
    match.refresh_from_db()
    assert match.home_score == INITIAL_SCORE


@pytest.mark.parametrize("team_count", [2, 3, 5, 8, 17, 128])
@pytest.mark.parametrize("batch_size", [None, 2])
def test_direct_draw_byes_preserve_all_entrants(
    team_count: int, batch_size: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every participant enters once; byes do not become fictitious scored matches."""
    owner = get_user_model().objects.create_user(username="draw-owner")
    tournament = Tournament.objects.create(
        name="Draw",
        slug="draw",
        owner=owner,
        starts_at=timezone.now(),
        cup_rules=deepcopy(RULES),
    )
    TournamentField.objects.create(tournament=tournament, label="A")
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=str(i), seed=i)
        for i in range(team_count)
    ]
    if batch_size is not None:
        monkeypatch.setattr(
            connection.ops, "bulk_batch_size", lambda fields, objs: batch_size
        )
    with CaptureQueriesContext(connection) as queries:
        generate_cup_draw(tournament)
    if batch_size is None:
        maximum_queries = 16
        assert len(queries) <= maximum_queries
    matches = list(tournament.matches.all())
    assert len(matches) == team_count - 1
    entrants = [
        team_id
        for match in matches
        for team_id in (match.home_team_id, match.away_team_id)
        if team_id
    ]
    assert set(entrants) == {team.pk for team in teams}
    assert len(entrants) == team_count
    assert sum(match.next_match_id is None for match in matches) == 1
    by_id = {match.pk: match for match in matches}
    destination_slots = []
    assert {match.match_number for match in matches} == set(range(1, team_count))
    for match in matches:
        assert match.created_at
        assert match.updated_at
        assert match.status == "scheduled"
        assert match.revision == 0
        if match.next_match_id:
            destination = by_id[match.next_match_id]
            assert destination.round_number == match.round_number + 1
            assert destination.starts_at > match.starts_at
            assert match.winner_to_side in {"home", "away"}
            destination_slots.append((destination.pk, match.winner_to_side))
    assert len(destination_slots) == len(set(destination_slots))
    with pytest.raises(CupError):
        generate_cup_draw(tournament)


def test_shootout_early_clinch_and_equal_attempt_sudden_death(cup: Tournament) -> None:
    """A sudden-death leader must allow the other team's matching attempt."""
    match = live_match(cup)
    state = cup_state(match)
    assert state is not None
    state["rules"]["shootout_attempts"] = 3
    state["attempts"] = [
        {"side": side, "scored": side == "home"}
        for _ in range(2)
        for side in ("home", "away")
    ]
    assert shootout_winner(state) == "home"
    state["rules"]["shootout_attempts"] = 1
    state["attempts"] = [
        {"side": "home", "scored": True},
        {"side": "away", "scored": True},
        {"side": "home", "scored": True},
    ]
    assert shootout_winner(state) is None
    state["attempts"].append({"side": "away", "scored": False})
    assert shootout_winner(state) == "home"


def test_period_boundary_prevents_undoing_an_earlier_half(
    client: Client, cup: Tournament
) -> None:
    """Undo cannot walk back through a confirmed phase boundary."""
    match = live_match(cup)
    match.home_score = 9
    match.save()
    goal_url = f"/api/tournaments/matches/{match.pk}/tracker/goal/"
    response = client.post(
        goal_url,
        {"side": "home", "expected_revision": match.revision},
        content_type="application/json",
    )
    old_event = response.json()["latest_event"]["id_uuid"]
    match.refresh_from_db()
    command(client, match, "advance")
    response = client.post(
        goal_url,
        {"side": "home", "expected_revision": match.revision},
        content_type="application/json",
    )
    latest = response.json()
    response = client.delete(
        f"/api/tournaments/matches/{match.pk}/tracker/events/latest/",
        {
            "event_id": latest["latest_event"]["id_uuid"],
            "expected_revision": latest["match"]["revision"],
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["latest_event"] is None
    response = client.delete(
        f"/api/tournaments/matches/{match.pk}/tracker/events/latest/",
        {
            "event_id": old_event,
            "expected_revision": response.json()["match"]["revision"],
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.CONFLICT


def test_setup_revision_and_rule_freeze(client: Client, cup: Tournament) -> None:
    """Existing fixtures cannot silently switch scoring rules."""
    rules = {**RULES, "shootout_attempts": 5}
    response = client.post(
        f"/api/tournaments/{cup.pk}/cup/",
        {"expected_revision": cup.live_revision, "rules": rules},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    cup.refresh_from_db()
    assert cup.cup_rules == RULES


def test_planning_rejects_cycles_and_stale_changes(
    client: Client, cup: Tournament
) -> None:
    """Unknown rounds can be assigned without weakening bracket integrity."""
    first = cup.matches.order_by("match_number").first()
    assert first is not None
    final = cup.matches.get(next_match__isnull=True)
    payload = {
        "expected_revision": final.revision,
        "round_name": "Finale",
        "round_number": 2,
        "starts_at": cup.starts_at.isoformat(),
        "field_id": str(cup.fields.get().pk),
        "next_match_id": str(first.pk),
        "winner_to_side": "home",
    }
    first.home_team = None
    first.save()
    url = f"/api/tournaments/matches/{final.pk}/cup/plan/"
    response = client.post(url, payload, content_type="application/json")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "cirkel" in response.json()["detail"]
    payload.update(next_match_id=None, winner_to_side="")
    assert (
        client.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.OK
    )
    assert (
        client.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.CONFLICT
    )


def test_cup_starts_individually_and_keeps_an_occupied_field_locked(
    client: Client, cup: Tournament
) -> None:
    """Sequential games on one field must not start as an entire knockout round."""
    first, second = list(cup.matches.order_by("match_number")[:2])
    for match in (first, second):
        match.field_ready_at = timezone.now()
        match.save()
    assert command(client, first, "start").status_code == HTTPStatus.OK
    assert command(client, second, "start").status_code == HTTPStatus.CONFLICT
    second.refresh_from_db()
    assert second.status == TournamentMatch.Status.SCHEDULED
    first.status = TournamentMatch.Status.FINAL
    first.save()
    assert command(client, second, "start").status_code == HTTPStatus.OK


def test_guest_referee_cannot_start_a_cup(client: Client, cup: Tournament) -> None:
    """A scoring credential does not grant the organizer's start capability."""
    match = cup.matches.order_by("match_number").first()
    assert match is not None
    match.field_ready_at = timezone.now()
    match.referee_claimed_at = timezone.now()
    match.referee_claim_token = uuid4()
    match.save()
    client.logout()
    response = client.post(
        f"/api/tournaments/matches/{match.pk}/tracker/cup/?token={match.referee_claim_token}",
        {"command": "start", "expected_revision": match.revision},
        content_type="application/json",
    )
    assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
    match.refresh_from_db()
    assert match.status == TournamentMatch.Status.SCHEDULED


def test_setup_generates_an_owned_draw(client: Client, cup: Tournament) -> None:
    """The organizer can create a cup through the public management API."""
    cup.matches.all().delete()
    cup.stages.all().delete()
    cup.cup_rules = None
    cup.save()
    response = client.post(
        f"/api/tournaments/{cup.pk}/cup/",
        {"expected_revision": cup.live_revision, "rules": RULES, "generate_draw": True},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json()["tournament"]["cup_rules"] == RULES
    assert len(response.json()["matches"]) == cup.teams.count() - 1
    assert response.json()["tournament"]["live_revision"] > cup.live_revision


def test_rejected_advancement_rolls_back_result_and_audit(
    client: Client, cup: Tournament
) -> None:
    """A downstream rejection must not commit a final without advancing its winner."""
    match = live_match(cup)
    destination = match.next_match
    destination.away_team = match.home_team
    destination.save()
    match.home_score = 11
    match.save()
    assert command(client, match, "advance").status_code == HTTPStatus.OK
    before = (
        match.status,
        match.revision,
        deepcopy(match.cup_state),
        match.result_audits.count(),
    )
    response = command(client, match, "advance")
    assert response.status_code == HTTPStatus.CONFLICT
    assert (
        match.status,
        match.revision,
        match.cup_state,
        match.result_audits.count(),
    ) == before
    assert response.json()["state"]["match"]["status"] == "live"
    cup.refresh_from_db()
    assert cup.live_revision == 1
    destination.refresh_from_db()
    assert destination.home_team_id is None


@pytest.mark.parametrize("phase", ["regular", "extra"])
def test_reopened_period_allows_goal_corrections_within_its_boundary(
    client: Client, cup: Tournament, phase: str
) -> None:
    """Reopening restores goal undo without reaching goals from a confirmed period."""
    match = live_match(cup)
    goal_url = f"/api/tournaments/matches/{match.pk}/tracker/goal/"
    undo_url = f"/api/tournaments/matches/{match.pk}/tracker/events/latest/"

    def goal() -> dict:
        response = client.post(
            goal_url,
            {"side": "home", "expected_revision": match.revision},
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.OK
        match.refresh_from_db()
        return response.json()

    if phase == "extra":
        for _ in range(2):
            assert command(client, match, "advance").status_code == HTTPStatus.OK
    goal()
    assert command(client, match, "advance").status_code == HTTPStatus.OK
    for _ in range(2):
        goal()
    assert command(client, match, "advance").status_code == HTTPStatus.OK
    closed_state = deepcopy(match.cup_state)
    assert match.status == "final"
    reset = client.post(
        f"/api/tournaments/matches/{match.pk}/state/reset/",
        {"expected_revision": match.revision},
        content_type="application/json",
    )
    assert reset.status_code == HTTPStatus.OK
    match.refresh_from_db()
    audit = match.result_audits.get(reason="Match state reset by tournament manager")
    assert audit.previous_cup_state == closed_state
    assert audit.new_cup_state == match.cup_state
    with CaptureQueriesContext(connection) as queries:
        state = client.get(f"/api/tournaments/matches/{match.pk}/tracker/").json()
        for _ in range(2):
            response = client.delete(
                undo_url,
                {
                    "event_id": state["latest_event"]["id_uuid"],
                    "expected_revision": state["match"]["revision"],
                },
                content_type="application/json",
            )
            assert response.status_code == HTTPStatus.OK
            state = response.json()
    assert_metadata_only_audit_reads(queries)
    assert state["latest_event"] is None
    assert state["match"]["home_score"] == INITIAL_SCORE + 1
    match.refresh_from_db()
    assert command(client, match, "advance").status_code == HTTPStatus.OK


def test_planning_converts_confirmed_entrant_to_winner_slot(
    client: Client, cup: Tournament
) -> None:
    """A confirmed occupied slot becomes a winner slot and advances normally."""
    match = cup.matches.order_by("match_number").first()
    assert match is not None
    destination = match.next_match
    destination.home_team = match.home_team
    destination.save()
    payload = {
        "expected_revision": match.revision,
        "round_name": match.stage.name,
        "round_number": match.round_number,
        "starts_at": match.starts_at.isoformat(),
        "field_id": str(match.field_id),
        "next_match_id": str(destination.pk),
        "winner_to_side": "home",
        "expected_destination_revision": destination.revision,
    }
    url = f"/api/tournaments/matches/{match.pk}/cup/plan/"
    assert (
        client.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.BAD_REQUEST
    )
    payload["replace_destination_team"] = True
    payload["expected_destination_revision"] = destination.revision + 1
    assert (
        client.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.BAD_REQUEST
    )
    destination.refresh_from_db()
    assert destination.home_team_id == match.home_team_id
    payload["expected_destination_revision"] = destination.revision
    assert (
        client.post(url, payload, content_type="application/json").status_code
        == HTTPStatus.OK
    )
    destination.refresh_from_db()
    assert destination.home_team_id is None
    assert destination.revision == 1
    match.refresh_from_db()
    match.status = TournamentMatch.Status.LIVE
    match.field_ready_at = timezone.now()
    match.home_score, match.away_score = 11, 10
    match.save()
    for _ in range(2):
        assert command(client, match, "advance").status_code == HTTPStatus.OK
    destination.refresh_from_db()
    assert destination.home_team_id == match.home_team_id


@pytest.mark.parametrize(
    "blocked", ["unrelated_team", "other_side", "other_feeder", "ready", "live"]
)
def test_planning_cannot_replace_unsafe_destination(
    client: Client, cup: Tournament, blocked: str
) -> None:
    """Confirmation cannot overwrite unrelated or started bracket data."""
    match, other, destination = list(cup.matches.order_by("match_number"))
    destination.home_team = match.home_team
    if blocked == "unrelated_team":
        destination.home_team = other.home_team
    elif blocked == "other_side":
        destination.away_team = match.away_team
    elif blocked == "other_feeder":
        other.winner_to_side = "home"
        other.save()
    elif blocked == "ready":
        destination.field_ready_at = timezone.now()
    else:
        destination.status = TournamentMatch.Status.LIVE
    destination.save()
    original = destination.home_team_id
    response = client.post(
        f"/api/tournaments/matches/{match.pk}/cup/plan/",
        {
            "expected_revision": match.revision,
            "round_name": match.stage.name,
            "round_number": 1,
            "starts_at": match.starts_at.isoformat(),
            "field_id": str(match.field_id),
            "next_match_id": str(destination.pk),
            "winner_to_side": "home",
            "replace_destination_team": True,
            "expected_destination_revision": destination.revision,
        },
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    destination.refresh_from_db()
    assert destination.home_team_id == original
    assert destination.revision == 0


@pytest.mark.parametrize("attempts", [6, 1000])
def test_shootout_tracker_keeps_audit_snapshots_out_of_goal_lookup(
    client: Client,
    cup: Tournament,
    attempts: int,
) -> None:
    """The read stays narrow while penalty audit history remains complete."""
    match = live_match(cup)
    state = cup_state(match)
    assert state is not None
    state.update(
        phase="shootout",
        shootout_first="home",
        attempts=[
            {"side": "home" if index % 2 == 0 else "away", "scored": False}
            for index in range(attempts)
        ],
    )
    match.cup_state = state
    match.save(update_fields=["cup_state"])
    assert (
        command(client, match, "penalty", side="home", scored=False).status_code
        == HTTPStatus.OK
    )
    with CaptureQueriesContext(connection) as queries:
        response = client.get(f"/api/tournaments/matches/{match.pk}/tracker/")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["latest_event"] is None
    assert len(response.json()["match"]["cup"]["attempts"]) == attempts + 1
    assert_metadata_only_audit_reads(queries)
    audit = match.result_audits.latest("created_at")
    assert audit.previous_cup_state == state
    assert audit.new_cup_state == match.cup_state
