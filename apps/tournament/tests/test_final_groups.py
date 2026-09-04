"""Multiple preplanned tournament final-group regression tests."""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client
import pytest

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)


pytestmark = pytest.mark.django_db

TOURNAMENT_DATE = "2026-09-04"
EXPECTED_BRACKET_MATCHES = 6
EXPECTED_POOL_MATCHES = 5


def _tournament_with_pools(
    client: Client,
) -> tuple[Tournament, dict[str, TournamentPool]]:
    owner = get_user_model().objects.create_user(username="final-group-manager")
    client.force_login(owner)
    tournament = Tournament.objects.create(
        name="BSF 2026",
        slug="bsf-2026-final-groups",
        owner=owner,
        timezone="Europe/Amsterdam",
        starts_at=datetime(2026, 9, 4, 19, 0, tzinfo=ZoneInfo("Europe/Amsterdam")),
        match_duration_minutes=10,
        changeover_minutes=2,
    )
    fields = [
        TournamentField.objects.create(
            tournament=tournament,
            label=f"Veld {index}",
            sort_order=index,
        )
        for index in range(1, 6)
    ]
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Poules",
        kind=TournamentStage.Kind.POOL,
        sort_order=1,
    )
    pools: dict[str, TournamentPool] = {}
    for index, letter in enumerate("ABCDE", start=1):
        pool = TournamentPool.objects.create(
            tournament=tournament,
            stage=stage,
            assigned_field=fields[index - 1],
            name=f"Poule {letter}",
            sort_order=index,
        )
        teams = [
            TournamentTeam.objects.create(
                tournament=tournament,
                name=f"{letter}{rank}",
                short_name=f"{letter}{rank}",
                seed=(index - 1) * 2 + rank,
            )
            for rank in (1, 2)
        ]
        TournamentPoolEntry.objects.bulk_create([
            TournamentPoolEntry(pool=pool, team=team, seed_order=rank)
            for rank, team in enumerate(teams, start=1)
        ])
        TournamentMatch.objects.create(
            tournament=tournament,
            stage=stage,
            pool=pool,
            field=fields[index - 1],
            home_team=teams[0],
            away_team=teams[1],
            starts_at=tournament.starts_at,
            duration_minutes=10,
            round_number=1,
            match_number=index,
        )
        pools[letter] = pool
    return tournament, pools


def _match_plan(field: TournamentField, start_time: str) -> dict[str, object]:
    return {
        "date": TOURNAMENT_DATE,
        "start_time": start_time,
        "field_id": str(field.id_uuid),
        "duration_minutes": 10,
    }


def _group_payload(
    name: str,
    format_name: str,
    pools: list[TournamentPool],
    fields: list[TournamentField],
) -> dict[str, object]:
    return {
        "name": name,
        "format": format_name,
        "pool_ids": [str(pool.id_uuid) for pool in pools],
        "semifinals": [
            _match_plan(fields[0], "20:36"),
            _match_plan(fields[1], "20:36"),
        ],
        "final": _match_plan(fields[0], "20:48"),
    }


def test_two_final_groups_can_be_preplanned_and_resolve_independently(
    client: Client,
) -> None:
    """The mixed and men's PDF brackets coexist and resolve from their own pools."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())
    mixed = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Gemengd",
            "three_pool_wildcard",
            [pools["A"], pools["B"], pools["E"]],
            fields[:2],
        ),
        content_type="application/json",
    )
    assert mixed.status_code == HTTPStatus.CREATED
    men = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["C"], pools["D"]],
            fields[2:4],
        ),
        content_type="application/json",
    )
    assert men.status_code == HTTPStatus.CREATED
    planned = men.json()
    assert [group["name"] for group in planned["final_groups"]] == [
        "Gemengd",
        "Heren",
    ]
    bracket_matches = [
        match for match in planned["matches"] if match["final_group_id"] is not None
    ]
    assert len(bracket_matches) == EXPECTED_BRACKET_MATCHES
    assert all(
        match["home_team"] is None and match["away_team"] is None
        for match in bracket_matches
    )
    assert bracket_matches[0]["home_source_label"] == "Poule A #1"
    assert bracket_matches[0]["away_source_label"] == "Poule B #1"
    assert bracket_matches[1]["home_source_label"] == "Poule E #1"
    assert bracket_matches[1]["away_source_label"] == (
        "Beste #2 van Poule A, Poule B, Poule E"
    )
    qualification_rules = {
        group["name"]: group["qualification_rules"] for group in planned["final_groups"]
    }
    assert qualification_rules["Gemengd"] == [
        {
            "kind": "pool_rank",
            "pool_ids": [str(pools["A"].id_uuid)],
            "rank": 1,
            "current_team_id": str(pools["A"].entries.get(seed_order=1).team_id),
            "is_decided": False,
        },
        {
            "kind": "pool_rank",
            "pool_ids": [str(pools["B"].id_uuid)],
            "rank": 1,
            "current_team_id": str(pools["B"].entries.get(seed_order=1).team_id),
            "is_decided": False,
        },
        {
            "kind": "pool_rank",
            "pool_ids": [str(pools["E"].id_uuid)],
            "rank": 1,
            "current_team_id": str(pools["E"].entries.get(seed_order=1).team_id),
            "is_decided": False,
        },
        {
            "kind": "best_rank",
            "pool_ids": [
                str(pools["A"].id_uuid),
                str(pools["B"].id_uuid),
                str(pools["E"].id_uuid),
            ],
            "rank": 2,
            "current_team_id": str(pools["A"].entries.get(seed_order=2).team_id),
            "is_decided": False,
        },
    ]

    scores = {"A": (2, 1), "B": (3, 0), "C": (5, 1), "D": (5, 1), "E": (4, 2)}
    for letter, pool in pools.items():
        match = pool.matches.get()
        response = client.patch(
            f"/api/tournaments/matches/{match.id_uuid}/result/",
            data={
                "home_score": scores[letter][0],
                "away_score": scores[letter][1],
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.OK

    snapshot = client.get(f"/api/tournaments/{tournament.id_uuid}/snapshot/").json()
    by_group = {
        group: [
            match
            for match in snapshot["matches"]
            if match["final_group_name"] == group
            and match["stage_kind"] == TournamentStage.Kind.KNOCKOUT
        ]
        for group in ("Gemengd", "Heren")
    }
    assert [
        (match["home_team"]["name"], match["away_team"]["name"])
        for match in by_group["Gemengd"]
    ] == [("A1", "B1"), ("E1", "A2")]
    assert [
        (match["home_team"]["name"], match["away_team"]["name"])
        for match in by_group["Heren"]
    ] == [("C2", "D1"), ("C1", "D2")]
    mixed_rules = next(
        group["qualification_rules"]
        for group in snapshot["final_groups"]
        if group["name"] == "Gemengd"
    )
    wildcard = next(rule for rule in mixed_rules if rule["kind"] == "best_rank")
    assert wildcard["current_team_id"] == str(
        pools["A"].entries.get(seed_order=2).team_id
    )
    assert all(rule["is_decided"] for rule in mixed_rules)

    heren_semifinals = TournamentMatch.objects.filter(
        stage__final_group__name="Heren",
        stage__kind=TournamentStage.Kind.KNOCKOUT,
    ).order_by("match_number")
    for semifinal in heren_semifinals:
        response = client.patch(
            f"/api/tournaments/matches/{semifinal.id_uuid}/result/",
            data={
                "home_score": 2,
                "away_score": 1,
                "status": "final",
                "winner_id": str(semifinal.home_team_id),
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.OK
    heren_final = TournamentMatch.objects.get(
        stage__final_group__name="Heren",
        stage__kind=TournamentStage.Kind.FINAL,
    )
    assert heren_final.home_team is not None
    assert heren_final.away_team is not None
    assert (heren_final.home_team.name, heren_final.away_team.name) == ("C2", "C1")

    pool_c_match = pools["C"].matches.get()
    correction = client.patch(
        f"/api/tournaments/matches/{pool_c_match.id_uuid}/result/",
        data={
            "home_score": 0,
            "away_score": 5,
            "status": "final",
            "expected_revision": 1,
        },
        content_type="application/json",
    )
    assert correction.status_code == HTTPStatus.CONFLICT
    pool_c_match.refresh_from_db()
    assert (pool_c_match.home_score, pool_c_match.away_score) == (5, 1)


def test_unstarted_final_group_can_be_deleted(client: Client) -> None:
    """An organizer can correct a planned bracket without deleting pool play."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["C"], pools["D"]],
            fields[2:4],
        ),
        content_type="application/json",
    )
    group_id = created.json()["final_groups"][0]["id_uuid"]

    deleted = client.delete(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/{group_id}/"
    )

    assert deleted.status_code == HTTPStatus.NO_CONTENT
    assert tournament.final_groups.count() == 0
    assert tournament.matches.filter(pool__isnull=True).count() == 0
    assert (
        tournament.matches.filter(pool__isnull=False).count() == EXPECTED_POOL_MATCHES
    )


def test_final_group_creation_locks_only_match_rows(client: Client) -> None:
    """Nullable team joins must not become PostgreSQL FOR UPDATE targets."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())

    with patch.object(
        TournamentMatch.objects,
        "select_for_update",
        wraps=TournamentMatch.objects.select_for_update,
    ) as select_for_update:
        created = client.post(
            f"/api/tournaments/{tournament.id_uuid}/final-groups/",
            data=_group_payload(
                "Heren",
                "two_pool_cross",
                [pools["C"], pools["D"]],
                fields[2:4],
            ),
            content_type="application/json",
        )

    assert created.status_code == HTTPStatus.CREATED
    select_for_update.assert_called_once_with(of=("self",))
