"""Real concurrent referee writes must serialize at the tournament aggregate."""

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.utils import timezone
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tournament.api.views import TournamentRefereeGoalView
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentStage,
    TournamentTeam,
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
)
def test_simultaneous_referee_goals_reject_the_stale_write() -> None:
    """Only one goal based on the same revision can reach the scoreboard."""
    owner = get_user_model().objects.create_user(username="concurrent-referee")
    tournament = Tournament.objects.create(
        name="Concurrency Cup",
        slug="concurrency-cup",
        owner=owner,
        starts_at=timezone.now(),
        status=Tournament.Status.PUBLISHED,
    )
    stage = TournamentStage.objects.create(
        tournament=tournament, name="Pools", kind=TournamentStage.Kind.POOL
    )
    field = TournamentField.objects.create(tournament=tournament, label="Field 1")
    home, away = [
        TournamentTeam.objects.create(tournament=tournament, name=name)
        for name in ("Home", "Away")
    ]
    match = TournamentMatch.objects.create(
        tournament=tournament,
        stage=stage,
        field=field,
        home_team=home,
        away_team=away,
        match_number=1,
        status=TournamentMatch.Status.LIVE,
        field_ready_at=timezone.now(),
    )
    barrier = Barrier(2, timeout=10)

    def goal(side: str) -> int:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            request = APIRequestFactory().post(
                "/", {"side": side, "expected_revision": 0}, format="json"
            )
            force_authenticate(request, user=owner)
            barrier.wait()
            return TournamentRefereeGoalView.as_view()(
                request, match_id=str(match.id_uuid)
            ).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(goal, ("home", "away")))
    assert sorted(results) == [HTTPStatus.OK, HTTPStatus.CONFLICT]
    match.refresh_from_db()
    assert (match.home_score or 0) + (match.away_score or 0) == 1
    assert match.revision == 1
