"""Planning requests must authorize and read defaults inside their write transaction."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from http import HTTPStatus
from queue import Queue
from time import monotonic, sleep
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import close_old_connections, connection, transaction
import pytest
from rest_framework.test import APIClient

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentFinalGroup,
    TournamentMatch,
    TournamentMember,
    TournamentPool,
    TournamentStandingAdjustment,
    TournamentTeam,
)
from apps.tournament.services.editing import create_pool


pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
    ),
]


@pytest.fixture
def tournament() -> Tournament:
    """Create an editable pool and two fields without any existing match schedule."""
    owner = get_user_model().objects.create(username="planning-owner")
    tournament = Tournament.objects.create(
        name="Planning",
        slug="planning",
        owner=owner,
        starts_at=datetime(2026, 9, 27, 10, tzinfo=UTC),
        timezone="UTC",
    )
    teams = [
        TournamentTeam.objects.create(tournament=tournament, name=f"Team {index}")
        for index in range(4)
    ]
    create_pool(tournament, name="A", team_ids=[team.pk for team in teams])
    for index in range(2):
        TournamentField.objects.create(tournament=tournament, label=f"Field {index}")
    return tournament


def _request(
    tournament: Tournament,
    path: str,
    *,
    method: str = "patch",
    data: dict[str, Any] | None = None,
    user: AbstractBaseUser | None = None,
) -> int:
    client = APIClient()
    client.force_authenticate(user=user or tournament.owner)
    response = getattr(client, method)(
        f"/api/tournaments/{tournament.pk}/{path}", data=data or {}, format="json"
    )
    return response.status_code


def _after_pending_change(
    tournament: Tournament,
    change: Callable[[], object],
    write: Callable[[], int],
) -> int:
    """Hold a pending aggregate edit until the competing request reaches a lock."""
    backend_ids: Queue[int] = Queue(maxsize=1)

    def competing_write() -> int:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend_ids.put(cursor.fetchone()[0])
            return write()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Tournament.objects.select_for_update().get(pk=tournament.pk)
            change()
            pending = executor.submit(competing_write)
            backend_id = backend_ids.get(timeout=5)
            deadline = monotonic() + 5
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))",
                        [backend_id],
                    )
                    if cursor.fetchone()[0]:
                        break
                assert monotonic() < deadline, (
                    "Request did not wait for the pending edit"
                )
                sleep(0.01)
        return pending.result(timeout=10)


def _match(tournament: Tournament) -> TournamentMatch:
    pool = tournament.pools.order_by("name").first()
    assert pool is not None
    home, away = [entry.team for entry in pool.entries.select_related("team")[:2]]
    return TournamentMatch.objects.create(
        tournament=tournament,
        stage=pool.stage,
        pool=pool,
        home_team=home,
        away_team=away,
        field=tournament.fields.first(),
        match_number=1,
        starts_at=tournament.starts_at,
        duration_minutes=10,
    )


@pytest.mark.parametrize("resource", ["pool", "match"])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_partial_planning_edits_preserve_pending_changes(
    tournament: Tournament,
    resource: str,
    change: str,
) -> None:
    """Defaults must not resurrect removed entrants or overwrite a new duration."""
    target = tournament.pools.get() if resource == "pool" else _match(tournament)
    target_id = target.pk
    first_team = tournament.teams.first()
    payload = {"name": "Renamed pool"} if resource == "pool" else {"round_number": 2}

    def pending_change() -> None:
        if change == "delete":
            target.delete()
        elif isinstance(target, TournamentPool):
            target.entries.filter(team=first_team).delete()
        else:
            target.duration_minutes *= 2
            target.save(update_fields=["duration_minutes"])

    collection = "pools" if resource == "pool" else "matches"
    status_code = _after_pending_change(
        tournament,
        pending_change,
        partial(_request, tournament, f"{collection}/{target_id}/", data=payload),
    )

    expected = HTTPStatus.OK if change == "edit" else HTTPStatus.NOT_FOUND
    assert status_code == expected
    if change == "delete":
        assert not type(target).objects.filter(pk=target_id).exists()
    elif isinstance(target, TournamentPool):
        assert not target.entries.filter(team=first_team).exists()
        target.refresh_from_db()
        assert target.name == payload["name"]
    else:
        expected_duration = target.duration_minutes
        target.refresh_from_db()
        assert target.duration_minutes == expected_duration
        assert target.round_number == payload["round_number"]


def test_publication_checks_the_committed_team_state(tournament: Tournament) -> None:
    """Withdrawing the last entrants must prevent a waiting publication."""
    _match(tournament)
    status_code = _after_pending_change(
        tournament,
        lambda: tournament.teams.update(withdrawn=True),
        partial(_request, tournament, "publish/", method="post"),
    )
    assert status_code == HTTPStatus.BAD_REQUEST
    tournament.refresh_from_db()
    assert tournament.status == Tournament.Status.DRAFT
    assert tournament.live_revision == 0


def test_display_patch_preserves_pending_announcement(tournament: Tournament) -> None:
    """Toggling one display option must retain another manager's announcement."""
    config = tournament.display_config

    def change_announcement() -> None:
        config.announcement = "Use the other entrance"
        config.save(update_fields=["announcement"])

    status_code = _after_pending_change(
        tournament,
        change_announcement,
        partial(_request, tournament, "display-config/", data={"show_live": False}),
    )
    assert status_code == HTTPStatus.OK
    config.refresh_from_db()
    assert config.announcement == "Use the other entrance"
    assert not config.show_live


def test_adjustment_rejects_an_entry_removed_while_waiting(
    tournament: Tournament,
) -> None:
    """An adjustment cannot retain a stale foreign key into a replaced pool."""
    entry = tournament.pools.get().entries.first()
    assert entry is not None
    entry_id = str(entry.pk)
    status_code = _after_pending_change(
        tournament,
        entry.delete,
        partial(
            _request,
            tournament,
            "adjustments/",
            method="post",
            data={"entry": entry_id, "points": 1, "reason": "Fair play"},
        ),
    )
    assert status_code == HTTPStatus.BAD_REQUEST
    assert not TournamentStandingAdjustment.objects.exists()


def _management_requests(
    tournament: Tournament,
) -> dict[str, tuple[str, str, dict[str, Any]]]:
    """Build valid requests for management routes that delegate locking to services."""
    pool = tournament.pools.get()
    fields = list(tournament.fields.all())
    extra_teams = [
        TournamentTeam.objects.create(tournament=tournament, name=f"Extra {index}")
        for index in range(4)
    ]
    second_pool = create_pool(
        tournament, name="B", team_ids=[team.pk for team in extra_teams[:2]]
    )
    match = _match(tournament)
    TournamentMatch.objects.create(
        tournament=tournament,
        stage=pool.stage,
        pool=second_pool,
        home_team=extra_teams[0],
        away_team=extra_teams[1],
        field=fields[1],
        match_number=2,
        starts_at=tournament.starts_at + timedelta(hours=1),
        duration_minutes=10,
    )
    group = TournamentFinalGroup.objects.create(
        tournament=tournament, name="Unused", format="two_pool_cross"
    )
    entry = pool.entries.first()
    assert entry is not None
    adjustment = TournamentStandingAdjustment.objects.create(
        entry=entry, points=1, reason="Fair play", created_by=tournament.owner
    )
    match_payload = {
        "pool_id": str(pool.pk),
        "home_team_id": str(match.home_team_id),
        "away_team_id": str(match.away_team_id),
        "field_id": str(fields[0].pk),
        "date": "2026-09-27",
        "start_time": "11:00",
        "duration_minutes": 10,
        "round_number": 2,
    }
    final_plan = {
        "date": "2026-09-27",
        "start_time": "12:00",
        "duration_minutes": 10,
    }
    return {
        "pool-create": (
            "post",
            "pools/",
            {
                "name": "C",
                "team_ids": [str(team.pk) for team in extra_teams[2:]],
            },
        ),
        "pool-edit": ("patch", f"pools/{pool.pk}/", {"name": "Renamed"}),
        "pool-delete": ("delete", f"pools/{pool.pk}/", {}),
        "match-create": ("post", "matches/", match_payload),
        "match-edit": ("patch", f"matches/{match.pk}/", {"round_number": 2}),
        "match-delete": ("delete", f"matches/{match.pk}/", {}),
        "import": (
            "post",
            "schedule/import/",
            {
                "rows": [
                    {
                        "date": "2026-09-27",
                        "start_time": "11:00",
                        "pool_name": "A",
                        "field_label": fields[0].label,
                        "home_team_name": match.home_team.name,
                        "away_team_name": match.away_team.name,
                    }
                ]
            },
        ),
        "publish": ("post", "publish/", {}),
        "display": ("patch", "display-config/", {"show_live": False}),
        "adjustment-create": (
            "post",
            "adjustments/",
            {
                "entry": str(entry.pk),
                "points": 1,
                "reason": "Fair play",
            },
        ),
        "adjustment-delete": ("delete", f"adjustments/{adjustment.pk}/", {}),
        "substitute": (
            "post",
            f"teams/{match.home_team_id}/substitutions/",
            {
                "replacements": [
                    {
                        "match_id": str(match.pk),
                        "substitute_team_id": str(extra_teams[0].pk),
                    }
                ],
            },
        ),
        "finals": ("post", "finals/generate/", {"qualifiers_per_pool": 2}),
        "final-group-create": (
            "post",
            "final-groups/",
            {
                "name": "Finals",
                "format": "two_pool_cross",
                "pool_ids": [str(pool.pk), str(second_pool.pk)],
                "semifinals": [
                    {**final_plan, "field_id": str(field.pk)} for field in fields
                ],
                "final": {
                    **final_plan,
                    "field_id": str(fields[0].pk),
                    "start_time": "12:30",
                },
            },
        ),
        "final-group-delete": ("delete", f"final-groups/{group.pk}/", {}),
    }


@pytest.mark.parametrize(
    "operation",
    [
        "pool-create",
        "pool-edit",
        "pool-delete",
        "match-create",
        "match-edit",
        "match-delete",
        "import",
        "publish",
        "display",
        "adjustment-create",
        "adjustment-delete",
        "substitute",
        "finals",
        "final-group-create",
        "final-group-delete",
    ],
)
@pytest.mark.parametrize("revoked", [False, True])
def test_waiting_management_request_rechecks_role(
    tournament: Tournament,
    operation: str,
    revoked: bool,
) -> None:
    """A manager revoked by the current lock owner cannot submit a waiting write."""
    manager = get_user_model().objects.create(username="requesting-manager")
    role = TournamentMember.objects.create(
        tournament=tournament, user=manager, role=TournamentMember.Role.MANAGER
    )
    method, path, payload = _management_requests(tournament)[operation]
    if operation.startswith("pool-"):
        tournament.matches.all().delete()
    status_code = _after_pending_change(
        tournament,
        role.delete if revoked else lambda: None,
        partial(_request, tournament, path, method=method, data=payload, user=manager),
    )
    created = {"pool-create", "match-create", "adjustment-create", "final-group-create"}
    expected = (
        HTTPStatus.CREATED
        if operation in created
        else HTTPStatus.NO_CONTENT
        if method == "delete"
        else HTTPStatus.OK
    )
    assert status_code == (HTTPStatus.FORBIDDEN if revoked else expected)
    tournament.refresh_from_db()
    assert tournament.live_revision == (0 if revoked else 1)
