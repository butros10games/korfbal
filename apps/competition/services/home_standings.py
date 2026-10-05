"""Poule standings of the signed-in player's own and followed teams, for Home."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db.models import F, Q
from django.utils import timezone
from django.utils.timezone import localtime

from apps.competition.models import (
    Match as SourceMatch,
    PoolEntry,
)
from apps.competition.services.standings import standing_values
from apps.player.models.player import Player
from apps.player.services.player_teams import grouped_teams_for_player
from apps.schedule.models import Match
from apps.team.models.team import Team


# Home shows a short list; followers of many teams keep their own teams first.
MAX_HOME_TEAMS = 6
# Teams without a standing must not use up those places, so more are examined.
MAX_CANDIDATE_TEAMS = 24
FORM_LENGTH = 5
# Source statuses of fixtures that will not be played at their listed time.
CALLED_OFF_STATUSES = ("SUSPENDED", "CANCELLED", "POSTPONED")

Row = dict[str, Any]


def _home_teams(player: Player) -> list[tuple[UUID, Team, str]]:
    """Return the player's teams once each: playing, then coaching, then followed."""
    groups = grouped_teams_for_player(player)
    teams: dict[UUID, tuple[UUID, Team, str]] = {}
    for role, queryset in (
        ("playing", groups.playing),
        ("coaching", groups.coaching),
        ("following", groups.following),
    ):
        for team in queryset[:MAX_CANDIDATE_TEAMS]:
            team_id = UUID(str(team.id_uuid))
            teams.setdefault(team_id, (team_id, team, role))
    return list(teams.values())[:MAX_CANDIDATE_TEAMS]


def _ranked_entries(team_ids: list[UUID]) -> dict[UUID, list[Row]]:
    """Return each team's ranked, published poules of the seasons running today."""
    today = timezone.localdate()
    rows = (
        PoolEntry.objects
        .filter(
            team__group__local_team_id__in=team_ids,
            pool__results_filtered=False,
            # The native season the poule is published in, not the import scope
            # of the source poule: only that one says whether it still runs.
            pool__local_pool__season__start_date__lte=today,
            pool__local_pool__season__end_date__gte=today,
        )
        .values(
            "standing",
            "pool_id",
            local_team=F("team__group__local_team_id"),
            local_pool=F("pool__local_pool_id"),
            pool_name=F("pool__name"),
            class_name=F("pool__class_name"),
        )
        .order_by("pool_id", "pk")
    )
    entries: dict[UUID, list[Row]] = defaultdict(list)
    seen: set[tuple[UUID, int]] = set()
    for row in rows:
        # Indoor and outdoor source entries of one team can share a poule.
        key = (row["local_team"], row["pool_id"])
        values = standing_values(row["standing"])
        if key in seen or values["position"] is None:
            continue
        seen.add(key)
        entries[row["local_team"]].append(row | {"values": values})
    return entries


def _pool_matches(
    team_ids: list[UUID], local_pools: set[UUID]
) -> dict[tuple[UUID, UUID], list[Row]]:
    """Return the teams' fixtures in their poules, oldest first, per team and poule."""
    wanted = set(team_ids)
    rows = (
        Match.objects
        .filter(pool_id__in=local_pools)
        .filter(Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids))
        .values(
            "id_uuid",
            "pool_id",
            "home_team_id",
            "away_team_id",
            "start_time",
            home_name=F("home_team__name"),
            home_club=F("home_team__club__name"),
            away_name=F("away_team__name"),
            away_club=F("away_team__club__name"),
            status=F("tracker_data__status"),
            home_score=F("tracker_data__home_score"),
            away_score=F("tracker_data__away_score"),
        )
        .order_by("start_time", "id_uuid")
    )
    rows = list(rows)
    # A called-off fixture keeps an upcoming tracker; it is not the next match.
    called_off = set(
        SourceMatch.objects
        .filter(
            local_match_id__in=[row["id_uuid"] for row in rows],
            status__in=CALLED_OFF_STATUSES,
        )
        .order_by()
        .values_list("local_match_id", flat=True)
    )
    matches: dict[tuple[UUID, UUID], list[Row]] = defaultdict(list)
    for row in rows:
        if row["id_uuid"] in called_off and row["status"] != "finished":
            continue
        for side in ("home", "away"):
            team_id = row[f"{side}_team_id"]
            if team_id in wanted:
                matches[team_id, row["pool_id"]].append(row | {"side": side})
    return matches


def _current_entry(
    entries: list[Row], matches: dict[UUID, list[Row]], now: datetime
) -> Row:
    """Pick the poule being played: the last one played in, else the next to start."""

    def rank(entry: Row) -> tuple[int, float, int]:
        rows = matches.get(entry["local_pool"], [])
        played = [row for row in rows if row["status"] == "finished"]
        if played:
            return (2, played[-1]["start_time"].timestamp(), entry["pool_id"])
        upcoming = [row for row in rows if row["start_time"] >= now]
        if upcoming:
            return (1, -upcoming[0]["start_time"].timestamp(), entry["pool_id"])
        return (0, 0.0, entry["pool_id"])

    return max(entries, key=rank)


def _form(matches: list[Row]) -> list[str]:
    """Return the latest results from the team's side, oldest first."""
    form: list[str] = []
    for row in matches:
        if row["status"] != "finished":
            continue
        own, other = (
            (row["home_score"], row["away_score"])
            if row["side"] == "home"
            else (row["away_score"], row["home_score"])
        )
        form.append("W" if own > other else "L" if own < other else "D")
    return form[-FORM_LENGTH:]


def _next_match(matches: list[Row], now: datetime) -> Row | None:
    """Return the first fixture of the poule that is still to be played."""
    return next(
        (
            row
            for row in matches
            if row["status"] != "finished" and row["start_time"] >= now
        ),
        None,
    )


def _pool_sizes(pool_ids: set[int]) -> dict[int, int]:
    """Count club teams per poule; indoor and outdoor source entries are one team."""
    members: dict[int, set[tuple[str, int]]] = defaultdict(set)
    for pool_id, source_team, group in (
        PoolEntry.objects
        .filter(pool_id__in=pool_ids)
        .order_by()
        .values_list("pool_id", "team_id", "team__group_id")
    ):
        members[pool_id].add(
            ("team", source_team) if group is None else ("group", group)
        )
    return {pool_id: len(teams) for pool_id, teams in members.items()}


def _opponent_positions(
    pool_ids: set[int], opponent_ids: set[UUID]
) -> dict[tuple[int, UUID], int]:
    """Return the rank of each next opponent in its poule, when it has one."""
    positions: dict[tuple[int, UUID], int] = {}
    if not opponent_ids:
        return positions
    for row in PoolEntry.objects.filter(
        pool_id__in=pool_ids, team__group__local_team_id__in=opponent_ids
    ).values("pool_id", "standing", local_team=F("team__group__local_team_id")):
        # A source variant without a rank must not hide the ranked one.
        position = standing_values(row["standing"])["position"]
        if position is not None:
            positions.setdefault((row["pool_id"], row["local_team"]), position)
    return positions


def home_team_standings(player: Player) -> list[Row]:
    """Return position, points, form and next opponent per team of the player."""
    teams = _home_teams(player)
    if not teams:
        return []
    team_ids = [team_id for team_id, _, _ in teams]
    entries = _ranked_entries(team_ids)
    if not entries:
        return []

    local_pools = {entry["local_pool"] for rows in entries.values() for entry in rows}
    matches = _pool_matches(team_ids, local_pools)
    now = timezone.now()

    # Only the teams Home shows need their poule sizes and opponents resolved.
    shown = [team_id for team_id in team_ids if team_id in entries][:MAX_HOME_TEAMS]
    chosen: dict[UUID, tuple[Row, list[Row], Row | None]] = {}
    for team_id in shown:
        rows = entries[team_id]
        by_pool = {
            pool: found for (owner, pool), found in matches.items() if owner == team_id
        }
        entry = _current_entry(rows, by_pool, now)
        pool_matches = by_pool.get(entry["local_pool"], [])
        chosen[team_id] = (entry, pool_matches, _next_match(pool_matches, now))

    pool_ids = {entry["pool_id"] for entry, _, _ in chosen.values()}
    sizes = _pool_sizes(pool_ids)
    opponent_side = {"home": "away", "away": "home"}
    opponent_ids = {
        upcoming[f"{opponent_side[upcoming['side']]}_team_id"]
        for _, _, upcoming in chosen.values()
        if upcoming
    }
    positions = _opponent_positions(pool_ids, opponent_ids)

    standings: list[Row] = []
    for team_id, team, role in teams:
        if team_id not in chosen:
            continue
        entry, pool_matches, upcoming = chosen[team_id]
        next_opponent: Row | None = None
        if upcoming:
            side = opponent_side[upcoming["side"]]
            next_opponent = {
                "match_id": str(upcoming["id_uuid"]),
                "start_time": localtime(upcoming["start_time"]).isoformat(),
                "name": upcoming[f"{side}_name"],
                "club": upcoming[f"{side}_club"],
                "position": positions.get((
                    entry["pool_id"],
                    upcoming[f"{side}_team_id"],
                )),
            }
        standings.append({
            "team": {
                "id_uuid": str(team_id),
                "name": team.name,
                "club": team.club.name,
                "logo_url": team.club.get_club_logo(),
            },
            "role": role,
            "pool": {
                "id": entry["pool_id"],
                "name": entry["pool_name"],
                "class_name": entry["class_name"],
            },
            "computed": entry["standing"].get("Computed") is True,
            "teams": sizes.get(entry["pool_id"], 0),
            **entry["values"],
            "form": _form(pool_matches),
            "next_opponent": next_opponent,
        })
    return standings[:MAX_HOME_TEAMS]
