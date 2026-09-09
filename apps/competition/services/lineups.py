"""Observed match selections reuse native people without manufacturing appearances."""

from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.competition.models import Match, MatchMembership, SyncResource
from apps.competition.services.match_timing import import_playing_time
from apps.competition.services.player_photos import discover_photo
from apps.competition.services.rosters import (
    count_private_people,
    parse_people,
    withdraw_people,
)
from apps.player.models import Player
from apps.schedule.models import Season


def queue_lineups(season: Season) -> int:
    """Queue one shared response per known fixture, without resetting checkpoints."""
    now = timezone.now()
    before = SyncResource.objects.filter(season=season, kind="match_lineup").count()
    SyncResource.objects.bulk_create(
        [
            SyncResource(
                season=season,
                kind="match_lineup",
                source_id=source_id,
                next_sync_at=max(now, starts_at - timedelta(days=1)),
            )
            for source_id, starts_at in Match.objects.filter(season=season).values_list(
                "external_id", "starts_at"
            )
        ],
        ignore_conflicts=True,
        batch_size=1000,
    )
    return (
        SyncResource.objects.filter(season=season, kind="match_lineup").count() - before
    )


@transaction.atomic
def import_lineup(
    season: Season, source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Replace a validated selection; absent starting-role support means unknown.

    Raises:
        ValueError: Identity, envelope or duplicate selections are inconsistent.
        TypeError: The starting-role capability is malformed.

    """
    match = (
        Match.objects
        .select_for_update()
        .filter(season=season, external_id=source_id)
        .select_related("home_team", "away_team")
        .first()
    )
    if match is None or data.get("PublicMatchId") != source_id:
        raise ValueError("Unrecognized match lineup")
    allows = data.get("AllowsBasePlayers")
    if not isinstance(allows, bool):
        raise TypeError("Missing starting-role capability")
    existing = MatchMembership.objects.filter(match=match)
    if match.lineup_observed_at and match.lineup_observed_at > observed_at:
        return
    import_playing_time(match, data, observed_at)
    selections, hidden = _parse_selections(match, data, allows)
    withdraw_people(hidden, season, observed_at)
    current = []
    for person_id, (team, name, privacy, roles, role, photo) in selections.items():
        if person_id in hidden:
            continue
        player, _ = Player.all_objects.get_or_create(
            knkv_person_id=person_id,
            defaults={"name": name, "knkv_observed_at": observed_at},
        )
        if player.knkv_observed_at and player.knkv_observed_at > observed_at:
            # Preserve a newer visible observation; never reintroduce a private one.
            if player.knkv_privacy in {"OPEN", "NORMAL", "LIMITED"}:
                current.extend(
                    existing.filter(player=player).values_list("pk", flat=True)
                )
            continue
        player.knkv_observed_at = observed_at
        player.knkv_privacy = privacy
        if player.user_id is None:
            player.name = name
        player.save(update_fields=("name", "knkv_privacy", "knkv_observed_at"))
        discover_photo(player, photo, season)
        observation, _ = MatchMembership.objects.update_or_create(
            match=match,
            player=player,
            defaults={
                "team": team,
                "roles": roles,
                "role": role,
                "observed_at": observed_at,
            },
        )
        current.append(observation.pk)
    existing.exclude(pk__in=current).delete()
    match.lineup_observed_at = observed_at
    match.private_lineup_counts = {
        side.lower(): count_private_people(
            [{**row, "TeamPerson": True} for row in data[side + "TeamPerson"]], hidden
        )
        for side in ("Home", "Away")
    }
    match.save(update_fields=("lineup_observed_at", "private_lineup_counts"))


def _starting_role(
    rows: list[dict], person_id: str, roles: list[str], allows: bool
) -> str:
    """Interpret BasePlayer only when the competition supports it.

    Raises:
        ValueError: Conflicting or unknown starting assignments are not substitutes.

    """
    if "PLAYER_DEFAULT" not in roles:
        return "staff"
    if not allows:
        return "selected"
    flags = [
        row.get("BasePlayer")
        for row in rows
        if row["PersonId"] == person_id
        and (row.get("TeamPersonFunction") or {}).get("RoleId") == "PLAYER_DEFAULT"
    ]
    if (
        not flags
        or not all(isinstance(flag, bool) for flag in flags)
        or any(flag != flags[0] for flag in flags)
    ):
        raise ValueError("Invalid starting-role assignment")
    return "starter" if flags[0] else "substitute"


def _parse_selections(
    match: Match, data: dict[str, Any], allows: bool
) -> tuple[dict, set[str]]:
    """Validate both teams before any privacy or membership changes.

    Raises:
        ValueError: A side or person identity conflicts with the known fixture.

    """
    selections = {}
    hidden = set()
    for side, team in (("Home", match.home_team), ("Away", match.away_team)):
        rows = data.get(side + "TeamPerson")
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
            or not isinstance(data.get(side + "Team"), dict)
            or data[side + "Team"].get("PublicTeamId") != team.external_id
        ):
            raise ValueError("Invalid match lineup side")
        # The match-side list itself establishes selection; TeamPerson is only
        # meaningful in TeamPersons and must never be interpreted as substitute.
        visible, private = parse_people([{**row, "TeamPerson": True} for row in rows])
        hidden.update(private)
        by_id = {row["PersonId"]: row for row in rows}
        for person_id, (name, _shirt, privacy, roles) in visible.items():
            if person_id in selections:
                raise ValueError("Person selected for both teams")
            role = _starting_role(rows, person_id, roles, allows)
            selections[person_id] = (
                team,
                name,
                privacy,
                roles,
                role,
                by_id[person_id].get("Photo"),
            )
    return selections, hidden
