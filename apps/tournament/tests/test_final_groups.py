"""Multiple preplanned tournament final-group regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from http import HTTPStatus
from itertools import combinations
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db.models import Q
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
EXPECTED_REMAINING_POOL_MATCHES = 3
EXPECTED_SEMIFINALS = 2


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


def _expand_pool_to_four_teams(
    tournament: Tournament,
    pool: TournamentPool,
) -> list[TournamentTeam]:
    pool.matches.all().delete()
    teams = list(pool.entries.select_related("team").order_by("seed_order"))
    tournament_teams = [entry.team for entry in teams]
    pool_suffix = pool.name.rsplit(maxsplit=1)[-1]
    for rank in (3, 4):
        team = TournamentTeam.objects.create(
            tournament=tournament,
            name=f"{pool_suffix}{rank}",
            short_name=f"{pool_suffix}{rank}",
            seed=rank,
        )
        TournamentPoolEntry.objects.create(
            pool=pool,
            team=team,
            seed_order=rank,
        )
        tournament_teams.append(team)

    next_number = tournament.matches.order_by("-match_number").first().match_number + 1
    for offset, (home, away) in enumerate(combinations(tournament_teams, 2)):
        TournamentMatch.objects.create(
            tournament=tournament,
            stage=pool.stage,
            pool=pool,
            field=pool.assigned_field,
            home_team=home,
            away_team=away,
            starts_at=tournament.starts_at + timedelta(minutes=12 * offset),
            duration_minutes=10,
            round_number=offset + 1,
            match_number=next_number + offset,
        )
    return tournament_teams


def test_secured_pool_winner_fills_semifinal_before_pool_finishes(
    client: Client,
) -> None:
    """An unreachable leader advances while lower-ranked teams still have games."""
    tournament, pools = _tournament_with_pools(client)
    teams = _expand_pool_to_four_teams(tournament, pools["A"])
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["A"], pools["B"]],
            fields[:2],
        ),
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED

    leader = teams[0]
    for match in (
        pools["A"]
        .matches.filter(
            home_team=leader,
        )
        .order_by("match_number")
    ):
        scored = client.patch(
            f"/api/tournaments/matches/{match.id_uuid}/result/",
            data={
                "home_score": 3,
                "away_score": 0,
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert scored.status_code == HTTPStatus.OK

    assert (
        pools["A"].matches.filter(status=TournamentMatch.Status.SCHEDULED).count()
        == EXPECTED_REMAINING_POOL_MATCHES
    )
    snapshot = client.get(f"/api/tournaments/{tournament.id_uuid}/snapshot/").json()
    semifinal = next(
        match
        for match in snapshot["matches"]
        if match["final_group_name"] == "Heren"
        and match["home_source_label"] == "Poule A #1"
    )
    assert semifinal["home_team"]["id_uuid"] == str(leader.id_uuid)
    rule = next(
        rule
        for rule in snapshot["final_groups"][0]["qualification_rules"]
        if rule["pool_ids"] == [str(pools["A"].id_uuid)] and rule["rank"] == 1
    )
    assert rule == {
        "kind": "pool_rank",
        "pool_ids": [str(pools["A"].id_uuid)],
        "rank": 1,
        "current_team_id": str(leader.id_uuid),
        "is_decided": True,
    }


def test_secured_wildcard_fills_before_source_pools_finish(client: Client) -> None:
    """A best second-place team advances once no remaining result can overtake it."""
    tournament, pools = _tournament_with_pools(client)
    selected_pools = [pools[letter] for letter in ("A", "B", "E")]
    expanded = {
        pool.name: _expand_pool_to_four_teams(tournament, pool)
        for pool in selected_pools
    }
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Gemengd",
            "three_pool_wildcard",
            selected_pools,
            fields[:2],
        ),
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED

    for pool in selected_pools:
        top_two = expanded[pool.name][:2]
        for match in pool.matches.filter(
            Q(home_team__in=top_two) | Q(away_team__in=top_two)
        ).order_by("match_number"):
            if match.home_team_id == top_two[0].id_uuid:
                scores = (1, 0)
            else:
                scores = (10, 0) if pool.name == "Poule A" else (2, 1)
            scored = client.patch(
                f"/api/tournaments/matches/{match.id_uuid}/result/",
                data={
                    "home_score": scores[0],
                    "away_score": scores[1],
                    "status": "final",
                    "expected_revision": 0,
                },
                content_type="application/json",
            )
            assert scored.status_code == HTTPStatus.OK

    snapshot = client.get(f"/api/tournaments/{tournament.id_uuid}/snapshot/").json()
    wildcard_match = next(
        match
        for match in snapshot["matches"]
        if match["away_source_label"] == "Beste #2 van Poule A, Poule B, Poule E"
    )
    assert wildcard_match["away_team"]["name"] == "A2"
    wildcard_rule = next(
        rule
        for rule in snapshot["final_groups"][0]["qualification_rules"]
        if rule["kind"] == "best_rank"
    )
    assert wildcard_rule["current_team_id"] == str(expanded["Poule A"][1].id_uuid)
    assert wildcard_rule["is_decided"] is True
    assert all(
        pool.matches.filter(status=TournamentMatch.Status.SCHEDULED).count() == 1
        for pool in selected_pools
    )


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
            "current_team_id": None,
            "is_decided": False,
        },
        {
            "kind": "pool_rank",
            "pool_ids": [str(pools["B"].id_uuid)],
            "rank": 1,
            "current_team_id": None,
            "is_decided": False,
        },
        {
            "kind": "pool_rank",
            "pool_ids": [str(pools["E"].id_uuid)],
            "rank": 1,
            "current_team_id": None,
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
            "current_team_id": None,
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
                "expected_revision": semifinal.revision,
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


def test_points_adjustment_refreshes_unstarted_semifinal_entrant(
    client: Client,
) -> None:
    """Audited table corrections cannot leave a stale qualifier in the bracket."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["A"], pools["B"]],
            fields[:2],
        ),
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    for pool in (pools["A"], pools["B"]):
        match = pool.matches.get()
        scored = client.patch(
            f"/api/tournaments/matches/{match.id_uuid}/result/",
            data={
                "home_score": 2,
                "away_score": 1,
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert scored.status_code == HTTPStatus.OK

    original_winner = pools["A"].entries.get(seed_order=1)
    replacement = pools["A"].entries.get(seed_order=2)
    semifinal = next(
        match
        for match in TournamentMatch.objects.filter(
            stage__final_group__name="Heren",
        )
        if match.home_qualifier
        == {
            "kind": "pool_rank",
            "pool_ids": [str(pools["A"].id_uuid)],
            "rank": 1,
        }
    )
    original_revision = semifinal.revision
    adjustment = client.post(
        f"/api/tournaments/{tournament.id_uuid}/adjustments/",
        data={
            "entry": str(original_winner.id_uuid),
            "points": -3,
            "reason": "Eligibility correction",
        },
        content_type="application/json",
    )
    assert adjustment.status_code == HTTPStatus.CREATED

    semifinal.refresh_from_db()
    assert semifinal.home_team_id == replacement.team_id
    assert semifinal.revision == original_revision + 1


def test_points_adjustment_rolls_back_when_qualifier_match_started(
    client: Client,
) -> None:
    """A ranking mutation cannot silently replace a team in a live semifinal."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["A"], pools["B"]],
            fields[:2],
        ),
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    for pool in (pools["A"], pools["B"]):
        match = pool.matches.get()
        client.patch(
            f"/api/tournaments/matches/{match.id_uuid}/result/",
            data={
                "home_score": 2,
                "away_score": 1,
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )

    original_winner = pools["A"].entries.get(seed_order=1)
    semifinal = next(
        match
        for match in TournamentMatch.objects.filter(
            stage__final_group__name="Heren",
        )
        if match.home_qualifier
        == {
            "kind": "pool_rank",
            "pool_ids": [str(pools["A"].id_uuid)],
            "rank": 1,
        }
    )
    original_team_id = semifinal.home_team_id
    semifinal.status = TournamentMatch.Status.LIVE
    semifinal.home_score = 0
    semifinal.away_score = 0
    semifinal.save(update_fields=["status", "home_score", "away_score"])

    adjustment = client.post(
        f"/api/tournaments/{tournament.id_uuid}/adjustments/",
        data={
            "entry": str(original_winner.id_uuid),
            "points": -3,
            "reason": "Late eligibility correction",
        },
        content_type="application/json",
    )
    assert adjustment.status_code == HTTPStatus.CONFLICT
    assert original_winner.adjustments.count() == 0
    semifinal.refresh_from_db()
    assert semifinal.home_team_id == original_team_id


def test_new_pool_fixture_clears_qualifiers_that_are_no_longer_secured(
    client: Client,
) -> None:
    """Schedule edits immediately invalidate bracket slots that became uncertain."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["A"], pools["B"]],
            fields[:2],
        ),
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    for pool in (pools["A"], pools["B"]):
        match = pool.matches.get()
        scored = client.patch(
            f"/api/tournaments/matches/{match.id_uuid}/result/",
            data={
                "home_score": 2,
                "away_score": 1,
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert scored.status_code == HTTPStatus.OK

    entries = list(pools["A"].entries.order_by("seed_order"))
    extra_match = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/",
        data={
            "pool_id": str(pools["A"].id_uuid),
            "home_team_id": str(entries[1].team_id),
            "away_team_id": str(entries[0].team_id),
            "field_id": str(pools["A"].assigned_field_id),
            "date": TOURNAMENT_DATE,
            "start_time": "19:12",
            "duration_minutes": 10,
            "round_number": 2,
        },
        content_type="application/json",
    )
    assert extra_match.status_code == HTTPStatus.CREATED
    a_sources = [
        match
        for match in extra_match.json()["matches"]
        if match["final_group_name"] == "Heren"
        and (
            match["home_source_label"] in {"Poule A #1", "Poule A #2"}
            or match["away_source_label"] in {"Poule A #1", "Poule A #2"}
        )
    ]
    assert len(a_sources) == EXPECTED_SEMIFINALS
    for match in a_sources:
        if match["home_source_label"].startswith("Poule A"):
            assert match["home_team"] is None
        if match["away_source_label"].startswith("Poule A"):
            assert match["away_team"] is None


def test_cancelled_only_pools_do_not_fill_qualifier_slots(client: Client) -> None:
    """A terminal schedule without played results has no arbitrary winner."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["A"], pools["B"]],
            fields[:2],
        ),
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    for pool in (pools["A"], pools["B"]):
        match = pool.matches.get()
        cancelled = client.patch(
            f"/api/tournaments/matches/{match.id_uuid}/result/",
            data={
                "home_score": None,
                "away_score": None,
                "status": "cancelled",
                "expected_revision": 0,
            },
            content_type="application/json",
        )
        assert cancelled.status_code == HTTPStatus.OK

    snapshot = client.get(f"/api/tournaments/{tournament.id_uuid}/snapshot/").json()
    semifinals = [
        match
        for match in snapshot["matches"]
        if match["final_group_name"] == "Heren"
        and match["stage_kind"] == TournamentStage.Kind.KNOCKOUT
    ]
    assert all(
        match["home_team"] is None and match["away_team"] is None
        for match in semifinals
    )
    assert all(
        rule["current_team_id"] is None and not rule["is_decided"]
        for rule in snapshot["final_groups"][0]["qualification_rules"]
    )


def test_corrected_semifinal_winner_resets_final_readiness(client: Client) -> None:
    """Changing a finalist invalidates readiness and advances the final revision."""
    tournament, pools = _tournament_with_pools(client)
    fields = list(tournament.fields.all())
    created = client.post(
        f"/api/tournaments/{tournament.id_uuid}/final-groups/",
        data=_group_payload(
            "Heren",
            "two_pool_cross",
            [pools["A"], pools["B"]],
            fields[:2],
        ),
        content_type="application/json",
    )
    assert created.status_code == HTTPStatus.CREATED
    for pool in (pools["A"], pools["B"]):
        match = pool.matches.get()
        client.patch(
            f"/api/tournaments/matches/{match.id_uuid}/result/",
            data={
                "home_score": 2,
                "away_score": 1,
                "status": "final",
                "expected_revision": 0,
            },
            content_type="application/json",
        )

    semifinals = list(
        TournamentMatch.objects.filter(
            stage__final_group__name="Heren",
            stage__kind=TournamentStage.Kind.KNOCKOUT,
        ).order_by("match_number")
    )
    for semifinal in semifinals:
        finished = client.patch(
            f"/api/tournaments/matches/{semifinal.id_uuid}/result/",
            data={
                "home_score": 2,
                "away_score": 1,
                "status": "final",
                "winner_id": str(semifinal.home_team_id),
                "expected_revision": semifinal.revision,
            },
            content_type="application/json",
        )
        assert finished.status_code == HTTPStatus.OK

    final = TournamentMatch.objects.get(
        stage__final_group__name="Heren",
        stage__kind=TournamentStage.Kind.FINAL,
    )
    ready = client.post(
        f"/api/tournaments/matches/{final.id_uuid}/readiness/",
        data={"expected_revision": final.revision},
        content_type="application/json",
    )
    assert ready.status_code == HTTPStatus.OK
    final.refresh_from_db()
    ready_revision = final.revision

    corrected = client.patch(
        f"/api/tournaments/matches/{semifinals[0].id_uuid}/result/",
        data={
            "home_score": 1,
            "away_score": 2,
            "status": "final",
            "winner_id": str(semifinals[0].away_team_id),
            "expected_revision": semifinals[0].revision + 1,
        },
        content_type="application/json",
    )
    assert corrected.status_code == HTTPStatus.OK
    final.refresh_from_db()
    assert final.field_ready_at is None
    assert final.revision == ready_revision + 1
    assert final.home_team_id == semifinals[0].away_team_id


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
