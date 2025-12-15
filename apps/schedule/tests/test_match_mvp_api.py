"""Tests for match MVP voting endpoints."""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, MatchPart, MatchPlayer
from apps.player.models.player import Player
from apps.schedule.models import Match, MatchMvp, Season
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_mvp_status_unavailable_before_finished(client: Client) -> None:
    """MVP status should be unavailable until match is finished."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)
    home_team = Team.objects.create(name="Home", club=Club.objects.create(name="HC"))
    away_team = Team.objects.create(name="Away", club=Club.objects.create(name="AC"))
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    response = client.get(f"/api/matches/{match.id_uuid}/mvp/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["available"] is False
    assert payload["open"] is False


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_mvp_vote_flow_and_publish_after_close(client: Client) -> None:
    """Authenticated vote should be persisted and publish after the window closes."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_team = Team.objects.create(name="Home", club=Club.objects.create(name="HC"))
    away_team = Team.objects.create(name="Away", club=Club.objects.create(name="AC"))
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    finished_at = timezone.now() - timezone.timedelta(minutes=2)
    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=finished_at - timezone.timedelta(minutes=30),
        end_time=finished_at,
        active=False,
    )

    # Create two users/players: voter + candidates
    user = get_user_model().objects.create_user(
        username="voter",
        password="pass1234",  # noqa: S106  # nosec
    )
    client.force_login(user)

    candidate_a_user = get_user_model().objects.create_user(
        username="alice",
        password="pass1234",  # noqa: S106  # nosec
    )
    candidate_b_user = get_user_model().objects.create_user(
        username="bob",
        password="pass1234",  # noqa: S106  # nosec
    )

    candidate_a: Player = candidate_a_user.player
    candidate_b: Player = candidate_b_user.player

    MatchPlayer.objects.create(
        match_data=match_data, team=home_team, player=candidate_a
    )
    MatchPlayer.objects.create(
        match_data=match_data, team=away_team, player=candidate_b
    )

    # Status should be open right after finish.
    response = client.get(f"/api/matches/{match.id_uuid}/mvp/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["available"] is True
    assert payload["open"] is True
    assert payload["user_vote"] is None
    assert any(c["id_uuid"] == str(candidate_a.id_uuid) for c in payload["candidates"])

    # Cast vote.
    response = client.post(
        f"/api/matches/{match.id_uuid}/mvp/vote/",
        data={"candidate_id_uuid": str(candidate_a.id_uuid)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["user_vote"]["candidate_id_uuid"] == str(candidate_a.id_uuid)

    # Force-close by setting MVP window to the past.
    mvp = MatchMvp.objects.get(match=match)
    mvp.finished_at = timezone.now() - timezone.timedelta(hours=9)
    mvp.closes_at = mvp.finished_at + timezone.timedelta(hours=8)
    mvp.save(update_fields=["finished_at", "closes_at", "updated_at"])

    # Next status call should publish.
    response = client.get(f"/api/matches/{match.id_uuid}/mvp/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["open"] is False
    assert payload["published_at"] is not None
    assert payload["mvp"]["id_uuid"] == str(candidate_a.id_uuid)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_mvp_anonymous_vote_persists_via_cookie(client: Client) -> None:
    """Anonymous votes should persist via signed cookie token."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_team = Team.objects.create(name="Home", club=Club.objects.create(name="HC"))
    away_team = Team.objects.create(name="Away", club=Club.objects.create(name="AC"))
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    finished_at = timezone.now() - timezone.timedelta(minutes=2)
    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=finished_at - timezone.timedelta(minutes=30),
        end_time=finished_at,
        active=False,
    )

    candidate_a_user = get_user_model().objects.create_user(
        username="alice",
        password="pass1234",  # noqa: S106  # nosec
    )
    candidate_b_user = get_user_model().objects.create_user(
        username="bob",
        password="pass1234",  # noqa: S106  # nosec
    )
    candidate_a: Player = candidate_a_user.player
    candidate_b: Player = candidate_b_user.player

    MatchPlayer.objects.create(
        match_data=match_data, team=home_team, player=candidate_a
    )
    MatchPlayer.objects.create(
        match_data=match_data, team=away_team, player=candidate_b
    )

    # Anonymous status should show open with no user vote.
    response = client.get(f"/api/matches/{match.id_uuid}/mvp/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["available"] is True
    assert payload["open"] is True
    assert payload["user_vote"] is None

    # Anonymous vote should be accepted and stored (cookie + DB).
    response = client.post(
        f"/api/matches/{match.id_uuid}/mvp/vote/",
        data={"candidate_id_uuid": str(candidate_a.id_uuid)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["user_vote"]["candidate_id_uuid"] == str(candidate_a.id_uuid)

    # A follow-up status call (same client, cookie retained) should show the vote.
    response = client.get(f"/api/matches/{match.id_uuid}/mvp/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["user_vote"]["candidate_id_uuid"] == str(candidate_a.id_uuid)

    # Anonymous user should be able to change their vote.
    response = client.post(
        f"/api/matches/{match.id_uuid}/mvp/vote/",
        data={"candidate_id_uuid": str(candidate_b.id_uuid)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["user_vote"]["candidate_id_uuid"] == str(candidate_b.id_uuid)

    # Force-close by setting MVP window to the past; winner should reflect the
    # anonymous vote.
    mvp = MatchMvp.objects.get(match=match)
    mvp.finished_at = timezone.now() - timezone.timedelta(hours=9)
    mvp.closes_at = mvp.finished_at + timezone.timedelta(hours=8)
    mvp.save(update_fields=["finished_at", "closes_at", "updated_at"])

    response = client.get(f"/api/matches/{match.id_uuid}/mvp/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["open"] is False
    assert payload["published_at"] is not None
    assert payload["mvp"]["id_uuid"] == str(candidate_b.id_uuid)
