"""Capture and rebuild match lineup projections from typed facts."""

from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import F

from apps.game_tracker.models import (
    GroupType,
    MatchData,
    PlayerChange,
    PlayerGroup,
    Shot,
    StartingPlayerAssignment,
)

from .player_groups import RESERVE_GROUP_NAME


def capture_starting_lineup(match_data: MatchData) -> int:
    """Persist the current groups once as the immutable starting lineup."""
    with transaction.atomic():
        locked = MatchData.objects.select_for_update().get(pk=match_data.pk)
        if StartingPlayerAssignment.objects.filter(match_data=locked).exists():
            return 0

        groups = list(
            PlayerGroup.objects
            .select_related("starting_type")
            .prefetch_related("players")
            .filter(match_data=locked)
        )
        groups.sort(key=lambda group: group.starting_type.name == RESERVE_GROUP_NAME)
        assignment_by_player: dict[str, StartingPlayerAssignment] = {}
        for group in groups:
            for player in group.players.all():
                assignment_by_player.setdefault(
                    str(player.pk),
                    StartingPlayerAssignment(
                        match_data=locked,
                        player_group=group,
                        player=player,
                    ),
                )
        StartingPlayerAssignment.objects.bulk_create(assignment_by_player.values())
        return len(assignment_by_player)


def starting_group_ids_by_player(match_data: MatchData) -> dict[str, str]:
    """Return the immutable starting group for each captured player."""
    return {
        str(player_id): str(player_group_id)
        for player_id, player_group_id in StartingPlayerAssignment.objects.filter(
            match_data=match_data
        ).values_list("player_id", "player_group_id")
    }


def rebuild_current_lineup(match_data: MatchData) -> None:
    """Rebuild mutable group membership from the start snapshot and substitutions."""
    with transaction.atomic():
        locked = MatchData.objects.select_for_update().get(pk=match_data.pk)
        if not StartingPlayerAssignment.objects.filter(match_data=locked).exists():
            capture_starting_lineup(locked)

        groups = list(
            PlayerGroup.objects.select_related("starting_type", "team").filter(
                match_data=locked
            )
        )
        group_by_id = {str(group.pk): group for group in groups}
        player_ids_by_group: dict[str, set[str]] = defaultdict(set)
        for player_id, group_id in StartingPlayerAssignment.objects.filter(
            match_data=locked
        ).values_list("player_id", "player_group_id"):
            player_ids_by_group[str(group_id)].add(str(player_id))

        reserve_group_by_team = {
            str(group.team_id): group
            for group in groups
            if group.starting_type.name == RESERVE_GROUP_NAME
        }
        changes = (
            PlayerChange.objects
            .select_related("player_group")
            .filter(match_data=locked)
            .order_by(F("time").asc(nulls_last=True), "id_uuid")
        )
        for change in changes:
            if change.player_in_id is None or change.player_out_id is None:
                continue
            target = group_by_id.get(str(change.player_group_id))
            if target is None:
                continue
            team_group_ids = {
                str(group.pk) for group in groups if group.team_id == target.team_id
            }
            player_in_id = str(change.player_in_id)
            player_out_id = str(change.player_out_id)
            for group_id in team_group_ids:
                player_ids_by_group[group_id].discard(player_in_id)
                player_ids_by_group[group_id].discard(player_out_id)
            player_ids_by_group[str(target.pk)].add(player_in_id)
            reserve = reserve_group_by_team.get(str(target.team_id))
            if reserve is not None:
                player_ids_by_group[str(reserve.pk)].add(player_out_id)

        for group in groups:
            group.players.set(player_ids_by_group.get(str(group.pk), set()))


def rebuild_group_roles(match_data: MatchData) -> None:
    """Rebuild attack/defence projection from starting roles and scored goals."""
    with transaction.atomic():
        locked = MatchData.objects.select_for_update().get(pk=match_data.pk)
        attack = GroupType.objects.filter(name="Aanval").first()
        defense = GroupType.objects.filter(name="Verdediging").first()
        if attack is None or defense is None:
            return
        swapped = (Shot.objects.filter(match_data=locked, scored=True).count() // 2) % 2
        for group in PlayerGroup.objects.filter(match_data=locked).select_related(
            "starting_type"
        ):
            desired_type_id = group.starting_type_id
            if swapped and group.starting_type_id == attack.pk:
                desired_type_id = defense.pk
            elif swapped and group.starting_type_id == defense.pk:
                desired_type_id = attack.pk
            if group.current_type_id != desired_type_id:
                group.current_type_id = desired_type_id
                group.save(update_fields=["current_type"])
