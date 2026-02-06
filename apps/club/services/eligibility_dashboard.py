"""Eligibility dashboard service for club admins."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from operator import itemgetter
import re
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.club.models import Club
from apps.game_tracker.models import MatchData, MatchPlayer, PlayerMatchMinutes
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


WEEK_START_ISO_DAY = 2  # Tuesday
INACTIVITY_RESET_DAYS = 45
MIN_MATCHES_FOR_RESTRICTIONS = 3
OWN_TEAM_PERCENT_THRESHOLD = 65
LOWER_TEAM_MAX_PLAYERS = 2


@dataclass(frozen=True)
class TeamContext:
    """Static contextual data used for eligibility checks per team."""

    team: Team
    wedstrijd_sport: bool
    team_rank: int
    family: str
    competition: str


@dataclass(frozen=True)
class PlayedEntry:
    """Single counted appearance entry for a player in a match week."""

    played_at: datetime
    week_start: date
    team_id: str
    team_rank: int
    family: str
    wedstrijd_sport: bool


@dataclass(frozen=True)
class PlayerState:
    """Computed eligibility state per player for one dashboard render."""

    player: Player
    counted_entries: list[PlayedEntry]
    total_counted: int
    restrictions_active: bool
    active_family: str | None
    own_team_id: str | None
    last_week_team_rank: int | None


def _week_start_for(dt: datetime) -> date:
    d = timezone.localtime(dt).date()
    diff = (d.isoweekday() - WEEK_START_ISO_DAY) % 7
    return d - timedelta(days=diff)


def _coerce_rank(raw_rank: int | None, team_name: str) -> int:
    if raw_rank is not None and raw_rank > 0:
        return int(raw_rank)

    match = re.search(r"(\d+)$", team_name.strip())
    if match:
        return max(1, int(match.group(1)))
    return 9999


def _infer_family(*, team_name: str, wedstrijd_sport: bool) -> str:
    normalized = team_name.upper().replace(" ", "")

    youth_u = re.match(r"U(19|17|15)(?:-|_)?\d+", normalized)
    if youth_u:
        return f"U{youth_u.group(1)}"

    if re.match(r"J\d+", normalized):
        return "J"

    if wedstrijd_sport:
        return "SENIOR_A"
    return "SENIOR_B"


def _expected_match_minutes(match_data: MatchData) -> float:
    parts = float(getattr(match_data, "parts", 0) or 0)
    part_length = float(getattr(match_data, "part_length", 0) or 0)
    expected = (parts * part_length) / 60.0
    return max(1.0, expected)


def _is_played_match(*, minutes_played: float, match_data: MatchData) -> bool:
    required = _expected_match_minutes(match_data) * 0.75
    return minutes_played >= required


def _pick_counted_match_for_week(entries: list[PlayedEntry]) -> PlayedEntry:
    a_entries = [entry for entry in entries if entry.wedstrijd_sport]
    candidates = a_entries or entries
    return max(candidates, key=lambda e: (e.team_rank, e.played_at))


def _trim_by_inactivity(entries: list[PlayedEntry]) -> list[PlayedEntry]:
    if len(entries) <= 1:
        return entries

    sorted_entries = sorted(entries, key=lambda e: e.played_at)
    last_cut = 0
    for idx in range(1, len(sorted_entries)):
        previous = sorted_entries[idx - 1]
        current = sorted_entries[idx]
        if (
            current.played_at.date() - previous.played_at.date()
        ).days > INACTIVITY_RESET_DAYS:
            last_cut = idx
    return sorted_entries[last_cut:]


def _select_active_family(entries: list[PlayedEntry]) -> str | None:
    if not entries:
        return None

    per_family: dict[str, int] = defaultdict(int)
    for entry in entries:
        per_family[entry.family] += 1

    max_count = max(per_family.values())
    leaders = {family for family, count in per_family.items() if count == max_count}
    if len(leaders) == 1:
        return next(iter(leaders))

    latest = max(entries, key=lambda e: e.played_at)
    return latest.family


def _threshold_passes(*, numerator: int, denominator: int) -> bool:
    if denominator <= 0:
        return False
    percentage_floor = (numerator * 100) // denominator
    return percentage_floor > OWN_TEAM_PERCENT_THRESHOLD


def _own_team_id(
    *,
    entries: list[PlayedEntry],
    teams: dict[str, TeamContext],
) -> str | None:
    if len(entries) < MIN_MATCHES_FOR_RESTRICTIONS:
        return None

    family = _select_active_family(entries)
    if family is None:
        return None

    family_entries = [entry for entry in entries if entry.family == family]
    if not family_entries:
        return None

    played_counts: dict[str, int] = defaultdict(int)
    for entry in family_entries:
        played_counts[entry.team_id] += 1

    ordered_teams = sorted(
        [ctx for ctx in teams.values() if ctx.family == family],
        key=lambda ctx: ctx.team_rank,
    )
    total = len(family_entries)
    for candidate in ordered_teams:
        in_team_or_higher = sum(
            count
            for team_id, count in played_counts.items()
            if team_id in teams
            and teams[team_id].family == candidate.family
            and teams[team_id].team_rank <= candidate.team_rank
        )
        if _threshold_passes(numerator=in_team_or_higher, denominator=total):
            return str(candidate.team.id_uuid)

    return None


def _distance_to_lock(*, current_q: int, current_n: int) -> int:
    if _threshold_passes(numerator=current_q, denominator=current_n):
        return 0
    for extra in range(1, 51):
        if _threshold_passes(
            numerator=current_q + extra,
            denominator=current_n + extra,
        ):
            return extra
    return 51


def _season_before_three_quarters(season: Season | None) -> bool:
    if season is None:
        return True
    today = timezone.localdate()
    total_days = (season.end_date - season.start_date).days
    if total_days <= 0:
        return False
    elapsed_days = (today - season.start_date).days
    return elapsed_days <= int(total_days * 0.75)


def _is_youth_family(family: str) -> bool:
    return family.startswith("U") or family == "J"


def _is_same_stage_family(a_family: str, b_family: str) -> bool:
    if _is_youth_family(a_family) and _is_youth_family(b_family):
        return True
    return a_family.startswith("SENIOR") and b_family.startswith("SENIOR")


def _can_lowest_a_play_b(
    *,
    own_team: TeamContext,
    target_team: TeamContext,
    lowest_a_rank_by_family: dict[str, int],
) -> bool:
    return (
        own_team.wedstrijd_sport
        and (not target_team.wedstrijd_sport)
        and own_team.team_rank == lowest_a_rank_by_family.get(own_team.family)
        and _is_same_stage_family(own_team.family, target_team.family)
    )


def _as_player_payload(player: Player) -> dict[str, str]:
    return {
        "id_uuid": str(player.id_uuid),
        "username": player.user.username,
        "profile_url": player.get_absolute_url(),
    }


def _build_teams_payload(
    team_context_by_id: dict[str, TeamContext],
) -> list[dict[str, Any]]:
    return [
        {
            "id_uuid": str(ctx.team.id_uuid),
            "name": ctx.team.name,
            "wedstrijd_sport": ctx.wedstrijd_sport,
            "team_rank": ctx.team_rank,
            "family": ctx.family,
        }
        for ctx in sorted(
            team_context_by_id.values(),
            key=lambda t: (t.family, t.team_rank, t.team.name.lower()),
        )
    ]


def _build_team_context_by_id(
    team_data_qs: QuerySet[TeamData],
) -> dict[str, TeamContext]:
    team_context_by_id: dict[str, TeamContext] = {}
    for row in team_data_qs:
        team_id = str(row.team.id_uuid)
        team_context_by_id[team_id] = TeamContext(
            team=row.team,
            wedstrijd_sport=bool(row.wedstrijd_sport),
            team_rank=_coerce_rank(getattr(row, "team_rank", None), row.team.name),
            family=_infer_family(
                team_name=row.team.name,
                wedstrijd_sport=bool(row.wedstrijd_sport),
            ),
            competition=(row.competition or "").strip(),
        )
    return team_context_by_id


def _fetch_match_data_by_id(
    *,
    club_team_ids: list[str],
    season: Season | None,
) -> dict[str, MatchData]:
    finished_matches_qs = (
        MatchData.objects
        .select_related("match_link", "match_link__season")
        .filter(status="finished", match_link__isnull=False)
        .filter(
            Q(match_link__home_team_id__in=club_team_ids)
            | Q(match_link__away_team_id__in=club_team_ids)
        )
    )
    if season is not None:
        finished_matches_qs = finished_matches_qs.filter(match_link__season=season)
    return {str(md.id_uuid): md for md in finished_matches_qs}


def _resolve_team_id_for_entry(
    *,
    match_data: MatchData,
    player_id: str,
    designated_team_by_match_and_player: dict[tuple[str, str], str],
    team_context_by_id: dict[str, TeamContext],
) -> str | None:
    team_id = designated_team_by_match_and_player.get((
        str(match_data.id_uuid),
        player_id,
    ))
    if team_id:
        return team_id
    if match_data.match_link is None:
        return None

    home_team_id = str(match_data.match_link.home_team_id)
    away_team_id = str(match_data.match_link.away_team_id)
    home_is_club = home_team_id in team_context_by_id
    away_is_club = away_team_id in team_context_by_id
    if home_is_club and not away_is_club:
        return home_team_id
    if away_is_club and not home_is_club:
        return away_team_id
    return home_team_id


def _collect_entries_and_players(
    *,
    match_data_by_id: dict[str, MatchData],
    club_team_ids: list[str],
    team_context_by_id: dict[str, TeamContext],
) -> tuple[dict[str, list[PlayedEntry]], dict[str, Player]]:
    match_minutes_qs = PlayerMatchMinutes.objects.select_related(
        "player",
        "player__user",
        "match_data",
        "match_data__match_link",
    ).filter(
        algorithm_version=LATEST_MATCH_MINUTES_VERSION,
        match_data_id__in=match_data_by_id.keys(),
    )

    designated_team_by_match_and_player = {
        (str(row.match_data_id), str(row.player_id)): str(row.team_id)
        for row in MatchPlayer.objects.filter(
            match_data_id__in=match_data_by_id.keys(),
            team_id__in=club_team_ids,
        ).only("match_data_id", "player_id", "team_id")
    }

    entries_by_player: dict[str, list[PlayedEntry]] = defaultdict(list)
    players_by_id: dict[str, Player] = {}

    for row in match_minutes_qs:
        match_data = row.match_data
        if match_data is None or match_data.match_link is None:
            continue

        player = row.player
        player_id = str(player.id_uuid)
        team_id = _resolve_team_id_for_entry(
            match_data=match_data,
            player_id=player_id,
            designated_team_by_match_and_player=designated_team_by_match_and_player,
            team_context_by_id=team_context_by_id,
        )
        if team_id is None or team_id not in team_context_by_id:
            continue
        if not _is_played_match(
            minutes_played=float(row.minutes_played),
            match_data=match_data,
        ):
            continue

        team_ctx = team_context_by_id[team_id]
        players_by_id[player_id] = player
        entries_by_player[player_id].append(
            PlayedEntry(
                played_at=match_data.match_link.start_time,
                week_start=_week_start_for(match_data.match_link.start_time),
                team_id=team_id,
                team_rank=team_ctx.team_rank,
                family=team_ctx.family,
                wedstrijd_sport=team_ctx.wedstrijd_sport,
            )
        )

    return entries_by_player, players_by_id


def _add_roster_players(
    players_by_id: dict[str, Player],
    team_data_qs: QuerySet[TeamData],
) -> None:
    roster_players = (
        Player.objects
        .filter(team_data_as_player__in=team_data_qs)
        .select_related("user")
        .distinct()
    )
    for player in roster_players:
        players_by_id.setdefault(str(player.id_uuid), player)


def _build_player_states(
    *,
    players_by_id: dict[str, Player],
    entries_by_player: dict[str, list[PlayedEntry]],
    team_context_by_id: dict[str, TeamContext],
) -> dict[str, PlayerState]:
    player_states: dict[str, PlayerState] = {}
    for player_id, player in players_by_id.items():
        raw_entries = entries_by_player.get(player_id, [])
        by_week: dict[date, list[PlayedEntry]] = defaultdict(list)
        for entry in raw_entries:
            by_week[entry.week_start].append(entry)

        counted_entries = [
            _pick_counted_match_for_week(entries)
            for _, entries in sorted(by_week.items(), key=itemgetter(0))
        ]
        counted_entries = _trim_by_inactivity(counted_entries)

        player_states[player_id] = PlayerState(
            player=player,
            counted_entries=counted_entries,
            total_counted=len(counted_entries),
            restrictions_active=len(counted_entries) >= MIN_MATCHES_FOR_RESTRICTIONS,
            active_family=_select_active_family(counted_entries),
            own_team_id=_own_team_id(entries=counted_entries, teams=team_context_by_id),
            last_week_team_rank=(
                counted_entries[-1].team_rank if counted_entries else None
            ),
        )
    return player_states


def _build_lower_team_slots(  # noqa: C901
    *,
    player_states: dict[str, PlayerState],
    team_context_by_id: dict[str, TeamContext],
    season: Season | None,
) -> dict[str, set[str]]:
    lower_team_slots: dict[str, set[str]] = defaultdict(set)
    if not _season_before_three_quarters(season):
        return lower_team_slots

    for target_ctx in team_context_by_id.values():
        if not target_ctx.wedstrijd_sport:
            continue
        source_rank = target_ctx.team_rank - 1
        if source_rank < 1:
            continue

        candidates: list[tuple[datetime, str]] = []
        for player_id, state in player_states.items():
            own_ctx = (
                team_context_by_id.get(state.own_team_id)
                if state.own_team_id is not None
                else None
            )
            if own_ctx is None:
                continue
            if not own_ctx.wedstrijd_sport:
                continue
            if own_ctx.family != target_ctx.family:
                continue
            if own_ctx.team_rank != source_rank:
                continue

            appearances = [
                entry.played_at
                for entry in state.counted_entries
                if entry.team_id == str(target_ctx.team.id_uuid)
            ]
            if not appearances:
                continue

            candidates.append((min(appearances), player_id))

        candidates.sort(key=itemgetter(0, 1))
        lower_team_slots[str(target_ctx.team.id_uuid)] = {
            pid for _, pid in candidates[:LOWER_TEAM_MAX_PLAYERS]
        }

    return lower_team_slots


def _build_lowest_a_rank_by_family(
    team_context_by_id: dict[str, TeamContext],
) -> dict[str, int]:
    lowest_a_rank_by_family: dict[str, int] = {}
    for ctx in team_context_by_id.values():
        if not ctx.wedstrijd_sport:
            continue
        current = lowest_a_rank_by_family.get(ctx.family)
        if current is None or ctx.team_rank > current:
            lowest_a_rank_by_family[ctx.family] = ctx.team_rank
    return lowest_a_rank_by_family


def _build_player_payloads(  # noqa: C901, PLR0912
    *,
    player_states: dict[str, PlayerState],
    team_context_by_id: dict[str, TeamContext],
    season: Season | None,
    lower_team_slots: dict[str, set[str]],
    lowest_a_rank_by_family: dict[str, int],
) -> list[dict[str, Any]]:
    players_payload: list[dict[str, Any]] = []
    for player_id, state in sorted(
        player_states.items(),
        key=lambda item: item[1].player.user.username.lower(),
    ):
        own_team = (
            team_context_by_id.get(state.own_team_id)
            if state.own_team_id is not None
            else None
        )

        by_team_rows: list[dict[str, Any]] = []
        for team_ctx in sorted(
            team_context_by_id.values(),
            key=lambda t: (t.family, t.team_rank, t.team.name.lower()),
        ):
            q = sum(
                1
                for entry in state.counted_entries
                if entry.family == team_ctx.family
                and entry.team_rank <= team_ctx.team_rank
            )
            n = sum(
                1 for entry in state.counted_entries if entry.family == team_ctx.family
            )
            percentage = math.floor(q * 100 / n) if n else 0

            same_class = bool(
                own_team
                and own_team.competition
                and team_ctx.competition
                and own_team.competition == team_ctx.competition
            )
            same_or_higher_from_own = bool(
                own_team and team_ctx.team_rank <= own_team.team_rank
            )
            same_or_higher_from_last_week = bool(
                state.last_week_team_rank is not None
                and team_ctx.team_rank <= state.last_week_team_rank
            )
            one_lower_than_own = bool(
                own_team and team_ctx.team_rank == own_team.team_rank + 1
            )

            if not state.restrictions_active:
                allowed = True
                reason = "Minder dan 3 gespeelde wedstrijden: geen beperkingen"
            elif own_team is None:
                allowed = False
                reason = "Eigen team nog niet te bepalen"
            elif _can_lowest_a_play_b(
                own_team=own_team,
                target_team=team_ctx,
                lowest_a_rank_by_family=lowest_a_rank_by_family,
            ):
                allowed = True
                reason = "Laagste A-team in leeftijdsgroep/senioren mag in B uitkomen"
            elif team_ctx.family != state.active_family:
                allowed = False
                reason = "Andere teamfamilie (leeftijdscategorie/senioriteit)"
            elif (
                own_team.wedstrijd_sport is False and team_ctx.wedstrijd_sport is False
            ):
                allowed = True
                reason = (
                    "Breedtesport: binnen B-categorie toegestaan (leeftijdscheck apart)"
                )
            elif own_team.wedstrijd_sport and (
                same_or_higher_from_own or same_class or same_or_higher_from_last_week
            ):
                allowed = True
                if same_class and not same_or_higher_from_own:
                    reason = "A-categorie: toegestaan in team met gelijke klasse"
                elif same_or_higher_from_last_week and not same_or_higher_from_own:
                    reason = "A-categorie: toegestaan op basis van laatste speelweek"
                else:
                    reason = "A-categorie: eigen team of hoger"
            elif own_team.wedstrijd_sport and one_lower_than_own:
                allowed = True
                reason = "A-categorie: 1 team lager (teamlimieten van toepassing)"
                if _season_before_three_quarters(season):
                    slot_players = lower_team_slots.get(
                        str(team_ctx.team.id_uuid),
                        set(),
                    )
                    if player_id not in slot_players:
                        allowed = False
                        reason = (
                            "A-categorie: limiet bereikt "
                            "(max 2 spelers van naaste hogere team)"
                        )
            else:
                allowed = False
                reason = "Niet toegestaan volgens huidige teamstatus"

            by_team_rows.append({
                "team_id": str(team_ctx.team.id_uuid),
                "team_name": team_ctx.team.name,
                "wedstrijd_sport": team_ctx.wedstrijd_sport,
                "team_rank": team_ctx.team_rank,
                "family": team_ctx.family,
                "played_ratio_percent": percentage,
                "distance_to_lock": _distance_to_lock(
                    current_q=q,
                    current_n=max(1, n),
                ),
                "allowed_for_team": allowed,
                "allowed_reason": reason,
            })

        players_payload.append({
            "player": _as_player_payload(state.player),
            "played_matches_count": state.total_counted,
            "restrictions_active": state.restrictions_active,
            "active_family": state.active_family,
            "own_team_id": str(own_team.team.id_uuid) if own_team else None,
            "own_team_name": own_team.team.name if own_team else None,
            "by_team": by_team_rows,
        })

    return players_payload


def build_club_eligibility_dashboard(
    *,
    club: Club,
    season: Season | None,
) -> dict[str, Any]:
    """Build club-level eligibility and vastspelen dashboard payload."""
    team_data_qs = TeamData.objects.select_related("team", "team__club").filter(
        team__club=club
    )
    if season is not None:
        team_data_qs = team_data_qs.filter(season=season)

    team_context_by_id = _build_team_context_by_id(team_data_qs)
    teams_payload = _build_teams_payload(team_context_by_id)
    if not team_context_by_id:
        return {
            "season_id": str(season.id_uuid) if season else None,
            "season_name": season.name if season else None,
            "generated_at": timezone.now().isoformat(),
            "teams": teams_payload,
            "players": [],
        }

    club_team_ids = list(team_context_by_id.keys())
    match_data_by_id = _fetch_match_data_by_id(
        club_team_ids=club_team_ids,
        season=season,
    )
    if not match_data_by_id:
        return {
            "season_id": str(season.id_uuid) if season else None,
            "season_name": season.name if season else None,
            "generated_at": timezone.now().isoformat(),
            "teams": teams_payload,
            "players": [],
        }

    entries_by_player, players_by_id = _collect_entries_and_players(
        match_data_by_id=match_data_by_id,
        club_team_ids=club_team_ids,
        team_context_by_id=team_context_by_id,
    )
    _add_roster_players(players_by_id, team_data_qs)

    player_states = _build_player_states(
        players_by_id=players_by_id,
        entries_by_player=entries_by_player,
        team_context_by_id=team_context_by_id,
    )
    lower_team_slots = _build_lower_team_slots(
        player_states=player_states,
        team_context_by_id=team_context_by_id,
        season=season,
    )
    lowest_a_rank_by_family = _build_lowest_a_rank_by_family(team_context_by_id)
    players_payload = _build_player_payloads(
        player_states=player_states,
        team_context_by_id=team_context_by_id,
        season=season,
        lower_team_slots=lower_team_slots,
        lowest_a_rank_by_family=lowest_a_rank_by_family,
    )

    return {
        "season_id": str(season.id_uuid) if season else None,
        "season_name": season.name if season else None,
        "generated_at": timezone.now().isoformat(),
        "teams": teams_payload,
        "players": players_payload,
    }
