"""Eligibility dashboard service for club admins."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
import math
from operator import itemgetter
import re
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.club.models import Club
from apps.club.queries.overview import eligibility_classifications
from apps.game_tracker.models import MatchData, MatchPlayer, PlayerMatchMinutes
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


WEEK_START_ISO_DAY = 2  # Tuesday
INACTIVITY_RESET_DAYS = 45
MIN_MATCHES_FOR_RESTRICTIONS = 3
OWN_TEAM_PERCENT_THRESHOLD = 65
SEASON_START_MONTH = 7
MODERN_YOUTH_START_YEAR = 2025
MINIMUM_AGE = 5


@dataclass(frozen=True)
class TeamContext:
    """Static contextual data used for eligibility checks per team."""

    team: Team
    wedstrijd_sport: bool
    team_rank: int
    family: str
    competition: str
    context: str = ""
    classification_known: bool = True


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
    history_needs_check: bool = False


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
    normalized = team_name.upper().strip()

    youth_u = re.search(r"(?:^|\s)U\s*(19|17|15)\s*[-_]?\s*\d+$", normalized)
    if youth_u:
        return f"U{youth_u.group(1)}"

    if re.search(r"(?:^|\s)J\s*\d+$", normalized):
        return "J"

    if not re.search(r"(?:^|\s)\d+$", normalized):
        return "UNKNOWN"
    if wedstrijd_sport:
        return "SENIOR_A"
    return "SENIOR_B"


def _team_order(family: str, rank: int) -> tuple[int, int]:
    """Article 21 orders senior teams before U19, U17 and U15."""
    return (
        {"SENIOR_A": 0, "SENIOR_B": 0, "U19": 1, "U17": 2, "U15": 3, "J": 4}.get(
            family, 5
        ),
        rank,
    )


def _expected_match_minutes(match_data: MatchData) -> float:
    parts = float(getattr(match_data, "parts", 0) or 0)
    part_length = float(getattr(match_data, "part_length", 0) or 0)
    expected = (parts * part_length) / 60.0
    return max(1.0, expected)


def _is_played_match(*, minutes_played: float, match_data: MatchData) -> bool:
    required = _expected_match_minutes(match_data) * 0.75
    return minutes_played >= required


def _pick_counted_match_for_week(entries: list[PlayedEntry]) -> PlayedEntry:
    return max(entries, key=lambda e: (_team_order(e.family, e.team_rank), e.played_at))


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
    played_teams = [teams[entry.team_id] for entry in entries if entry.team_id in teams]
    contexts = {team.context for team in played_teams}
    if len(contexts) != 1 or any(
        not team.classification_known for team in played_teams
    ):
        return None

    played_counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        played_counts[entry.team_id] += 1

    ordered_teams = sorted(
        [
            team
            for team in teams.values()
            if team.context in contexts and team.classification_known
        ],
        key=lambda ctx: _team_order(ctx.family, ctx.team_rank),
    )
    total = len(entries)
    for candidate in ordered_teams:
        in_team_or_higher = sum(
            count
            for team_id, count in played_counts.items()
            if team_id in teams
            and _team_order(teams[team_id].family, teams[team_id].team_rank)
            <= _team_order(candidate.family, candidate.team_rank)
        )
        if _threshold_passes(numerator=in_team_or_higher, denominator=total):
            return str(candidate.team.id_uuid)

    return None


def _distance_to_lock(*, current_q: int, current_n: int) -> int:
    if current_n >= MIN_MATCHES_FOR_RESTRICTIONS and _threshold_passes(
        numerator=current_q, denominator=current_n
    ):
        return 0
    for extra in range(1, 51):
        if current_n + extra >= MIN_MATCHES_FOR_RESTRICTIONS and _threshold_passes(
            numerator=current_q + extra,
            denominator=current_n + extra,
        ):
            return extra
    return 51


def _as_player_payload(player: Player) -> dict[str, str]:
    return {
        "id_uuid": str(player.id_uuid),
        "username": player.display_name,
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
            "classification_known": ctx.classification_known,
        }
        for ctx in sorted(
            team_context_by_id.values(),
            key=lambda t: (_team_order(t.family, t.team_rank), t.team.name.lower()),
        )
    ]


def _build_team_context_by_id(
    team_data_qs: QuerySet[TeamData],
) -> dict[str, TeamContext]:
    classifications = eligibility_classifications(team_data_qs)

    team_context_by_id: dict[str, TeamContext] = {}
    for row in team_data_qs:
        team_id = str(row.team.id_uuid)
        family = _infer_family(
            team_name=row.team.name, wedstrijd_sport=bool(row.wedstrijd_sport)
        )
        wedstrijd_sport = bool(row.wedstrijd_sport)
        competition = (row.competition or "").strip()
        context = ""
        known = family != "UNKNOWN" and not (
            (family.startswith("U") and not wedstrijd_sport)
            or (family == "J" and wedstrijd_sport)
        )
        observed = classifications.get(row.pk, set())
        if len(observed) == 1:
            category, age_group, competition, context = next(iter(observed))
            wedstrijd_sport = category in {"top", "a"}
            family = (
                ("SENIOR_A" if wedstrijd_sport else "SENIOR_B")
                if age_group == "senior"
                else "J"
                if age_group == "youth"
                else age_group
            )
            known = category in {"top", "a", "b"} and family in {
                "SENIOR_A",
                "SENIOR_B",
                "U19",
                "U17",
                "U15",
                "J",
            }
        elif len(observed) > 1:
            known = False
        if family not in {"SENIOR_A", "SENIOR_B", "U19", "U17", "U15", "J"}:
            family = "UNKNOWN"
        team_context_by_id[team_id] = TeamContext(
            team=row.team,
            wedstrijd_sport=wedstrijd_sport,
            team_rank=_coerce_rank(None if observed else row.team_rank, row.team.name),
            family=family,
            competition=competition,
            context=context,
            classification_known=known,
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
        .filter(status="finished", match_link__start_time__lte=timezone.now())
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
    # A derby without player/team attribution cannot identify an appearance.
    return None


def _collect_entries_and_players(
    *,
    match_data_by_id: dict[str, MatchData],
    team_context_by_id: dict[str, TeamContext],
) -> tuple[dict[str, list[PlayedEntry]], dict[str, Player]]:
    match_minutes_qs = PlayerMatchMinutes.objects.select_related(
        "player",
        "player__user",
        "match_data",
        "match_data__match_link",
    ).filter(
        player__in=Player.objects.all(),
        algorithm_version=LATEST_MATCH_MINUTES_VERSION,
        match_data_id__in=match_data_by_id.keys(),
    )

    designated_team_by_match_and_player = {
        (str(row.match_data_id), str(row.player_id)): str(row.team_id)
        for row in MatchPlayer.objects.filter(
            match_data_id__in=match_data_by_id.keys(),
        ).only("match_data_id", "player_id", "team_id")
    }

    entries_by_player: dict[str, list[PlayedEntry]] = defaultdict(list)
    players_by_id: dict[str, Player] = {}

    for row in match_minutes_qs:
        match_data = match_data_by_id.get(str(row.match_data.pk))
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
        if (
            _age_check(
                player,
                team_ctx,
                match_data.match_link.season,
                on=timezone.localtime(match_data.match_link.start_time).date(),
            )[0]
            == "blocked"
        ):
            continue
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
        # Sportlink settles a week on Tuesday. The current week cannot change
        # permission yet, including the week of the third qualifying appearance.
        counted_entries = [
            entry
            for entry in counted_entries
            if entry.week_start < _week_start_for(timezone.now())
        ]
        history = sorted(raw_entries, key=lambda entry: entry.played_at)
        history_needs_check = any(
            (right.played_at.date() - left.played_at.date()).days
            >= INACTIVITY_RESET_DAYS
            for left, right in pairwise(history)
        )
        if (
            history
            and (timezone.localdate() - history[-1].played_at.date()).days
            >= INACTIVITY_RESET_DAYS
        ):
            history_needs_check = True

        player_states[player_id] = PlayerState(
            player=player,
            history_needs_check=history_needs_check,
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


def _build_lowest_a_rank_by_family(
    team_context_by_id: dict[str, TeamContext],
) -> dict[tuple[str, str], int]:
    lowest_a_rank_by_family: dict[tuple[str, str], int] = {}
    for ctx in team_context_by_id.values():
        if not ctx.wedstrijd_sport:
            continue
        current = lowest_a_rank_by_family.get((ctx.context, ctx.family))
        if current is None or ctx.team_rank > current:
            lowest_a_rank_by_family[ctx.context, ctx.family] = ctx.team_rank
    return lowest_a_rank_by_family


def _age_check(
    player: Player, team: TeamContext, season: Season | None, *, on: date | None = None
) -> tuple[str, str]:
    """Check privately held dates; never infer an age from roster or J-number.

    KNKV RvW art. 6 and competition handbook 7.2/7.3.3:
    https://www.knkv.nl/kennisbank/competitiehandboek/
    Published B cutoffs are not available locally. Rounded allocation averages
    cannot replace either official cutoff (ordinary or A-bound substitutes).
    """
    born = player.date_of_birth
    today = on or timezone.localdate()
    if (
        born is not None
        and (
            today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        )
        < MINIMUM_AGE
    ):
        return "blocked", "De minimumleeftijd voor competitie is 5 jaar."
    if team.family in {"U19", "U17", "U15"}:
        return _youth_age_check(player, team, season)
    if team.family == "J":
        return "check", (
            "Controleer de geboortedatum en de officiële KNKV-invallersgrens "
            "van dit jeugdteam. "
            "Bij vastspelen in A geldt de strengere grens op basis van de teamleeftijd."
        )
    if team.family.startswith("SENIOR"):
        return "passed", "Geen bovengrens voor leeftijd bij senioren."
    return "check", "Leeftijdscategorie van het team is niet vastgesteld."


def _youth_age_check(
    player: Player, team: TeamContext, season: Season | None
) -> tuple[str, str]:
    born = player.date_of_birth
    if season is None:
        return "check", "Kies een seizoen om de geboortejaargrens te bepalen."
    year = season.start_date.year - (season.start_date.month < SEASON_START_MONTH)
    if year < MODERN_YOUTH_START_YEAR:
        return (
            "check",
            "Controleer de leeftijdsregels voor dit historische seizoen.",
        )
    earliest_year = year - int(team.family[1:]) + 1
    if born is None:
        return (
            "check",
            f"Geboortedatum ontbreekt; {team.family} vereist geboortejaar "
            f"{earliest_year} of later.",
        )
    if born.year < earliest_year:
        return (
            "blocked",
            f"Buiten de geboortejaargrens van {team.family} "
            f"({earliest_year} of later).",
        )
    return "passed", f"Voldoet aan de geboortejaargrens van {team.family}."


def _binding_check(
    state: PlayerState,
    target: TeamContext,
    teams: dict[str, TeamContext],
    lowest_a: dict[tuple[str, str], int],
) -> tuple[str, str]:
    """Evaluate article 21 without pretending to know a future match lineup."""
    own = teams.get(state.own_team_id or "")
    if not target.classification_known or any(
        not teams[entry.team_id].classification_known for entry in state.counted_entries
    ):
        return (
            "check",
            "Competitie-indeling ontbreekt of bevat meerdere competitiedelen. "
            "Controleer Sportlink.",
        )
    if any(
        teams[entry.team_id].context != target.context
        for entry in state.counted_entries
    ):
        return (
            "check",
            "Andere competitiecontext; controleer de speelstatus in Sportlink.",
        )
    if state.history_needs_check:
        return (
            "check",
            "Onderbreking van 45 dagen of langer: controleer de herstartstatus "
            "en eventuele veldpauze in Sportlink.",
        )
    if not state.restrictions_active or own is None:
        return (
            ("available", "Nog geen 3 meegetelde speelweken; geen vastspeelbeperking.")
            if not state.restrictions_active
            else ("check", "Eigen team is nog niet te bepalen.")
        )
    if not target.wedstrijd_sport:
        return _b_binding_check(own, lowest_a)
    return _a_binding_check(state, own, target, teams)


def _b_binding_check(
    own: TeamContext, lowest_a: dict[tuple[str, str], int]
) -> tuple[str, str]:
    if not own.wedstrijd_sport:
        return (
            "available",
            "Geen vastspeelbeperking binnen B; de leeftijdsregels blijven gelden.",
        )
    if own.team_rank == lowest_a.get((own.context, own.family)):
        return (
            "available",
            "Laagste A-team van de leeftijdscategorie; "
            "de B-leeftijdsregels blijven gelden.",
        )
    return (
        "blocked",
        "Vastgespeeld in A, boven het laagste A-team van de leeftijdscategorie.",
    )


def _a_binding_check(
    state: PlayerState,
    own: TeamContext,
    target: TeamContext,
    teams: dict[str, TeamContext],
) -> tuple[str, str]:
    target_order = _team_order(target.family, target.team_rank)
    own_order = _team_order(own.family, own.team_rank)
    if own.competition.casefold() in {
        "league:standard",
        "korfbal league",
    } and target.competition.casefold() in {"league:reserve", "reserve korfbal league"}:
        return "available", "Korfbal League naar Reserve Korfbal League (artikel 21.9)."
    if target_order <= own_order:
        return "available", "Eigen team of een hoger team in de KNKV-volgorde."
    if (
        own.wedstrijd_sport
        and own.family == target.family
        and own.competition
        and own.competition == target.competition
    ):
        return "available", "Het team speelt in dezelfde klasse als het eigen team."
    last = state.counted_entries[-1] if state.counted_entries else None
    if last and target_order <= _team_order(last.family, last.team_rank):
        return (
            "available",
            "Toegestaan op basis van het team in de laatste meegetelde speelweek.",
        )
    lower_teams = sorted(
        (
            team
            for team in teams.values()
            if team.wedstrijd_sport
            and team.context == own.context
            and _team_order(team.family, team.team_rank) > own_order
        ),
        key=lambda team: _team_order(team.family, team.team_rank),
    )
    if own.wedstrijd_sport and lower_teams and lower_teams[0] == target:
        return "check", (
            "Eén team lager: maximaal 2 spelers uit het naaste hogere team "
            "per wedstrijd, "
            "alleen tot ¾ van de teamcompetitie. Controleer speelronde en opstelling."
        )
    return (
        "blocked",
        "Lager dan toegestaan op basis van het eigen team en de laatste speelweek.",
    )


def _build_player_payloads(
    *,
    player_states: dict[str, PlayerState],
    team_context_by_id: dict[str, TeamContext],
    season: Season | None,
    roster_teams: dict[str, list[str]],
    lowest_a_rank_by_family: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    players_payload: list[dict[str, Any]] = []
    for player_id, state in sorted(
        player_states.items(), key=lambda item: item[1].player.display_name.lower()
    ):
        own_team = (
            None
            if state.history_needs_check
            else team_context_by_id.get(state.own_team_id or "")
        )
        by_team_rows: list[dict[str, Any]] = []
        for team_ctx in sorted(
            team_context_by_id.values(),
            key=lambda t: _team_order(t.family, t.team_rank),
        ):
            status, reason = _binding_check(
                state, team_ctx, team_context_by_id, lowest_a_rank_by_family
            )
            age_status, age_reason = _age_check(state.player, team_ctx, season)
            if age_status == "blocked":
                status = "blocked"
            elif age_status == "check" and status != "blocked":
                status = "check"
            if state.total_counted == 0 and status == "available":
                status = "check"
                reason = (
                    "Geen meegetelde speelminuten beschikbaar; "
                    "controleer de speelgeschiedenis in Sportlink."
                )
            n = state.total_counted
            q = sum(
                1
                for entry in state.counted_entries
                if _team_order(entry.family, entry.team_rank)
                <= _team_order(team_ctx.family, team_ctx.team_rank)
            )
            comparable = (
                team_ctx.wedstrijd_sport
                and team_ctx.classification_known
                and not state.history_needs_check
                and all(
                    team_context_by_id[entry.team_id].classification_known
                    and team_context_by_id[entry.team_id].context == team_ctx.context
                    for entry in state.counted_entries
                )
            )
            by_team_rows.append({
                "team_id": str(team_ctx.team.id_uuid),
                "team_name": team_ctx.team.name,
                "wedstrijd_sport": team_ctx.wedstrijd_sport,
                "team_rank": team_ctx.team_rank,
                "family": team_ctx.family,
                "played_ratio_percent": math.floor(q * 100 / n)
                if n and comparable
                else None,
                "distance_to_lock": _distance_to_lock(current_q=q, current_n=n)
                if comparable and status != "blocked"
                else None,
                "allowed_for_team": status == "available",
                "eligibility_status": status,
                "allowed_reason": reason,
                "age_status": age_status,
                "age_reason": age_reason,
            })
        players_payload.append({
            "player": _as_player_payload(state.player),
            "birth_date_known": state.player.date_of_birth is not None,
            "roster_team_ids": roster_teams.get(player_id, []),
            "played_matches_count": state.total_counted,
            "restrictions_active": state.restrictions_active,
            "active_family": own_team.family if own_team else None,
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
    entries_by_player, players_by_id = _collect_entries_and_players(
        match_data_by_id=match_data_by_id,
        team_context_by_id=team_context_by_id,
    )
    _add_roster_players(players_by_id, team_data_qs)

    player_states = _build_player_states(
        players_by_id=players_by_id,
        entries_by_player=entries_by_player,
        team_context_by_id=team_context_by_id,
    )
    roster_teams: dict[str, list[str]] = defaultdict(list)
    for team_id, player_id in team_data_qs.values_list("team_id", "players__pk"):
        if player_id is not None:
            roster_teams[str(player_id)].append(str(team_id))
    lowest_a_rank_by_family = _build_lowest_a_rank_by_family(team_context_by_id)
    players_payload = _build_player_payloads(
        player_states=player_states,
        team_context_by_id=team_context_by_id,
        season=season,
        roster_teams=roster_teams,
        lowest_a_rank_by_family=lowest_a_rank_by_family,
    )

    return {
        "season_id": str(season.id_uuid) if season else None,
        "season_name": season.name if season else None,
        "generated_at": timezone.now().isoformat(),
        "teams": teams_payload,
        "players": players_payload,
    }
