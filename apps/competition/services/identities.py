"""Stable source identity keys for joint registrations and unnamed poules."""

from collections import defaultdict
import re

from apps.competition.models import Team, TeamGroup


def team_group_key(name: str, club_name: str) -> str:
    """Ignore partner ordering only for an exact club and team designation."""
    value = " ".join(name.casefold().split())
    partners, separator, designation = value.rpartition(" ")
    parts = [part.strip() for part in partners.split("/")]
    club = " ".join(club_name.casefold().split())
    if (
        separator
        and len(parts) > 1
        and all(parts)
        and club in parts
        and re.fullmatch(r"[a-z]*\d+", designation)
    ):
        return f"{'/'.join(sorted(parts))} {designation}"
    return value


def merge_unlinked_joint_groups() -> int:
    """Join source aliases only when they cannot combine distinct native records.

    The caller holds the provider lease and transaction. Preserve every source team
    ID and the linked global team/season roster; remove only redundant source groups.
    """
    buckets: dict[tuple, list[TeamGroup]] = defaultdict(list)
    for group in TeamGroup.objects.select_for_update().select_related("club"):
        key = team_group_key(group.name, group.club.name)
        buckets[group.season_id, group.club.pk, key].append(group)
    merged = 0
    for (_, _, key), groups in buckets.items():
        linked = [
            group for group in groups if group.local_team_id or group.local_team_data_id
        ]
        if len(linked) > 1:
            continue
        survivor = linked[0] if linked else min(groups, key=lambda group: group.pk)
        for group in groups:
            if group.pk == survivor.pk:
                continue
            Team.objects.filter(group=group).update(group=survivor)
            group.delete()
            merged += 1
        if survivor.normalized_name != key:
            survivor.normalized_name = key
            survivor.save(update_fields=("normalized_name",))
    return merged


def unnamed_pool_label(external_id: str) -> str:
    """Keep nameless source poules distinct by their provider identity."""
    return f"KNKV-poule {external_id}"
