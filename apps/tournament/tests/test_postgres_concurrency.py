"""Concurrent tournament writes must serialize at the aggregate."""

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from queue import Queue
from threading import Barrier
from time import monotonic, sleep

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tournament.api.views import TournamentRefereeGoalView
from apps.tournament.api.views.planning import (
    TournamentGenerationApplyView,
    TournamentMatchesGenerateView,
    TournamentPoolsGenerateView,
)
from apps.tournament.api.views.resources import (
    TournamentFieldDetailView,
    TournamentFieldListCreateView,
    TournamentMemberDetailView,
    TournamentMemberListCreateView,
    TournamentTeamDetailView,
    TournamentTeamListCreateView,
)
from apps.tournament.api.views.tournaments import TournamentViewSet
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentMember,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.editing import create_pool, update_pool_order


def _writer_backend_id() -> int:
    """Identify the competing connection and bound any unexpected lock wait."""
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '10s'")
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _wait_for_blocked_writer(backend_id: int) -> None:
    """Wait until the other database connection is blocked by this transaction."""
    deadline = monotonic() + 5
    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [backend_id]
            )
            if cursor.fetchone()[0]:
                return
        assert monotonic() < deadline, "Writer did not wait for the aggregate lock"
        sleep(0.01)


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


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
)
@pytest.mark.parametrize("resource_kind", ["field", "team"])
def test_resource_deletion_waits_for_pending_schedule(resource_kind: str) -> None:
    """A deletion cannot race a planner holding the aggregate lock into data loss."""
    owner = get_user_model().objects.create_user(username="concurrent-planner")
    tournament = Tournament.objects.create(
        name="Deletion Cup",
        slug="deletion-cup",
        owner=owner,
        starts_at=timezone.now(),
    )
    stage = TournamentStage.objects.create(
        tournament=tournament, name="Pools", kind=TournamentStage.Kind.POOL
    )
    field = TournamentField.objects.create(tournament=tournament, label="Field 1")
    home, away = [
        TournamentTeam.objects.create(tournament=tournament, name=name)
        for name in ("Home", "Away")
    ]
    backend_ids: Queue[int] = Queue(maxsize=1)

    def delete_resource() -> int:
        close_old_connections()
        try:
            backend_ids.put(_writer_backend_id())
            request = APIRequestFactory().delete("/")
            force_authenticate(request, user=owner)
            view, resource_id = (
                (TournamentFieldDetailView, field.pk)
                if resource_kind == "field"
                else (TournamentTeamDetailView, home.pk)
            )
            return view.as_view()(
                request,
                tournament_id=str(tournament.pk),
                **{f"{resource_kind}_id": str(resource_id)},
            ).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Tournament.objects.select_for_update().get(pk=tournament.pk)
            deletion = executor.submit(delete_resource)
            backend_id = backend_ids.get(timeout=5)
            _wait_for_blocked_writer(backend_id)
            assert TournamentField.objects.filter(pk=field.pk).exists()
            assert TournamentTeam.objects.filter(pk=home.pk).exists()
            match = TournamentMatch.objects.create(
                tournament=tournament,
                stage=stage,
                field=field,
                home_team=home,
                away_team=away,
                match_number=1,
            )
        assert deletion.result(timeout=10) == HTTPStatus.CONFLICT

    match.refresh_from_db()
    assert match.field_id == field.pk
    assert match.home_team_id == home.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
)
@pytest.mark.parametrize("generation", ["pools", "matches", "combined"])
def test_generation_reads_committed_planning_inputs(generation: str) -> None:
    """Generation must not apply old teams or pool indexes after a pending edit."""
    owner = get_user_model().objects.create_user(username="generation-manager")
    tournament = Tournament.objects.create(
        name="Generation Cup",
        slug="generation-cup",
        owner=owner,
        starts_at=timezone.now(),
    )
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=f"Team {index}")
        for index in range(4)
    ]
    TournamentField.objects.create(tournament=tournament, label="Field 1")
    first_pool = create_pool(
        tournament, name="A", team_ids=[team.pk for team in teams[:2]]
    )
    second_pool = create_pool(
        tournament, name="B", team_ids=[team.pk for team in teams[2:]]
    )
    backend_ids: Queue[int] = Queue(maxsize=1)

    def generate() -> int:
        close_old_connections()
        try:
            backend_ids.put(_writer_backend_id())
            view = {
                "pools": TournamentPoolsGenerateView,
                "matches": TournamentMatchesGenerateView,
                "combined": TournamentGenerationApplyView,
            }[generation]
            payload = {} if generation == "matches" else {"pool_count": 1}
            request = APIRequestFactory().post("/", payload, format="json")
            force_authenticate(request, user=owner)
            return view.as_view()(request, tournament_id=str(tournament.pk)).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Tournament.objects.select_for_update().get(pk=tournament.pk)
            if generation == "matches":
                update_pool_order(
                    tournament, first_pool, sort_order=second_pool.sort_order + 1
                )
            else:
                teams[0].withdrawn = True
                teams[0].save(update_fields=["withdrawn"])
            pending = executor.submit(generate)
            _wait_for_blocked_writer(backend_ids.get(timeout=5))
        assert pending.result(timeout=10) == HTTPStatus.OK

    if generation != "matches":
        assert set(
            TournamentPoolEntry.objects.filter(pool__tournament=tournament).values_list(
                "team_id", flat=True
            )
        ) == {team.pk for team in teams[1:]}
    for match in tournament.matches.select_related("pool"):
        assert match.pool is not None
        entrants = set(match.pool.entries.values_list("team_id", flat=True))
        assert {match.home_team_id, match.away_team_id} <= entrants


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
)
@pytest.mark.parametrize("change", ["grant", "revoke", "direct-revoke"])
def test_member_write_respects_pending_grant_or_revocation(change: str) -> None:
    """A competing grant stays unchanged and a revoked role cannot be resurrected."""
    owner = get_user_model().objects.create_user(username="grant-owner")
    user = get_user_model().objects.create_user(username="grant-recipient")
    tournament = Tournament.objects.create(
        name="Roles", slug="roles", owner=owner, starts_at=timezone.now()
    )
    existing = (
        TournamentMember.objects.create(
            tournament=tournament, user=user, role=TournamentMember.Role.SCOREKEEPER
        )
        if change != "grant"
        else None
    )
    member_id = existing.pk if existing else None
    backend_ids: Queue[int] = Queue(maxsize=1)

    def write_member() -> int:
        close_old_connections()
        try:
            backend_ids.put(_writer_backend_id())
            factory = APIRequestFactory()
            request = (factory.post if change == "grant" else factory.patch)(
                "/", {"user": user.pk, "role": "manager"}, format="json"
            )
            force_authenticate(request, user=owner)
            if change == "grant":
                return TournamentMemberListCreateView.as_view()(
                    request, tournament_id=str(tournament.pk)
                ).status_code
            return TournamentMemberDetailView.as_view()(
                request, tournament_id=str(tournament.pk), member_id=member_id
            ).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            if change != "direct-revoke":
                Tournament.objects.select_for_update().get(pk=tournament.pk)
            if existing:
                existing.delete()
            else:
                TournamentMember.objects.create(
                    tournament=tournament,
                    user=user,
                    role=TournamentMember.Role.SCOREKEEPER,
                )
            pending = executor.submit(write_member)
            _wait_for_blocked_writer(backend_ids.get(timeout=5))
        expected = HTTPStatus.BAD_REQUEST if change == "grant" else HTTPStatus.NOT_FOUND
        assert pending.result(timeout=10) == expected

    if change == "grant":
        assert tournament.member_roles.get(user=user).role == (
            TournamentMember.Role.SCOREKEEPER
        )
    else:
        assert not tournament.member_roles.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
)
def test_tournament_patch_preserves_concurrent_fields_and_revision() -> None:
    """A partial edit must read the latest values before saving the tournament."""
    owner = get_user_model().objects.create_user(username="edit-owner")
    tournament = Tournament.objects.create(
        name="Original name",
        slug="concurrent-edits",
        owner=owner,
        starts_at=timezone.now(),
    )
    backend_ids: Queue[int] = Queue(maxsize=1)

    def edit_location() -> tuple[int, dict]:
        close_old_connections()
        try:
            backend_ids.put(_writer_backend_id())
            request = APIRequestFactory().patch(
                "/", {"location": "New venue"}, format="json"
            )
            force_authenticate(request, user=owner)
            response = TournamentViewSet.as_view({"patch": "partial_update"})(
                request, id_uuid=str(tournament.pk)
            )
            return response.status_code, response.data
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            current = Tournament.objects.select_for_update().get(pk=tournament.pk)
            current.name = "Concurrent name"
            current.live_revision = 1
            current.save(update_fields=["name", "live_revision"])
            pending = executor.submit(edit_location)
            _wait_for_blocked_writer(backend_ids.get(timeout=5))
        status_code, payload = pending.result(timeout=10)

    assert status_code == HTTPStatus.OK
    tournament.refresh_from_db()
    assert tournament.name == payload["name"] == "Concurrent name"
    assert tournament.location == payload["location"] == "New venue"
    assert (
        tournament.live_revision
        == payload["live_revision"]
        == current.live_revision + 1
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
)
@pytest.mark.parametrize("resource_kind", ["team", "field"])
@pytest.mark.parametrize("change", ["edit", "delete", "create"])
def test_resource_writes_respect_pending_changes(
    resource_kind: str, change: str
) -> None:
    """Writes preserve committed state and reject names that became unavailable."""
    owner = get_user_model().objects.create_user(username="resource-owner")
    tournament = Tournament.objects.create(
        name="Resources", slug="resources", owner=owner, starts_at=timezone.now()
    )
    resource = (
        TournamentTeam.objects.create(tournament=tournament, name="Team 1")
        if resource_kind == "team"
        else TournamentField.objects.create(tournament=tournament, label="Field 1")
    )
    resource_id = resource.pk
    backend_ids: Queue[int] = Queue(maxsize=1)

    def write_resource() -> int:
        close_old_connections()
        try:
            backend_ids.put(_writer_backend_id())
            key = "name" if resource_kind == "team" else "label"
            factory = APIRequestFactory()
            request = (factory.post if change == "create" else factory.patch)(
                "/", {key: "New name"}, format="json"
            )
            force_authenticate(request, user=owner)
            view = (
                {
                    "team": TournamentTeamListCreateView,
                    "field": TournamentFieldListCreateView,
                }
                if change == "create"
                else {
                    "team": TournamentTeamDetailView,
                    "field": TournamentFieldDetailView,
                }
            )[resource_kind]
            resource_kwargs = (
                {} if change == "create" else {f"{resource_kind}_id": str(resource_id)}
            )
            return view.as_view()(
                request,
                tournament_id=str(tournament.pk),
                **resource_kwargs,
            ).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Tournament.objects.select_for_update().get(pk=tournament.pk)
            if change == "delete":
                resource.delete()
            elif change == "create":
                key = "name" if resource_kind == "team" else "label"
                setattr(resource, key, "New name")
                resource.save(update_fields=[key])
            elif isinstance(resource, TournamentTeam):
                resource.withdrawn = True
                resource.save(update_fields=["withdrawn"])
            else:
                resource.active = False
                resource.save(update_fields=["active"])
            pending = executor.submit(write_resource)
            _wait_for_blocked_writer(backend_ids.get(timeout=5))
        expected = {
            "edit": HTTPStatus.OK,
            "delete": HTTPStatus.NOT_FOUND,
            "create": HTTPStatus.BAD_REQUEST,
        }[change]
        assert pending.result(timeout=10) == expected

    if change == "delete":
        assert not type(resource).objects.filter(pk=resource_id).exists()
    elif change == "create":
        assert (
            type(resource).objects.filter(tournament=tournament).get().pk == resource_id
        )
    else:
        resource.refresh_from_db()
        if isinstance(resource, TournamentTeam):
            assert resource.withdrawn
            assert resource.name == "New name"
        else:
            assert not resource.active
            assert resource.label == "New name"
