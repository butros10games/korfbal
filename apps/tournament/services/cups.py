"""Cup rules and audited match phases, under the tournament aggregate lock."""

from copy import deepcopy
from typing import Any

from apps.tournament.models import TournamentMatch, TournamentResultAudit
from apps.tournament.services.match_operations import (
    downstream_result_locked,
    start_round,
    sync_advanced_winner,
)


MAX_PERIODS = 4
MAX_PERIOD_MINUTES = 120
MAX_REGULAR_MINUTES = 240
MAX_SHOOTOUT_ATTEMPTS = 20


class CupError(ValueError):
    """The requested cup action cannot be applied to the current state."""


def validate_cup_rules(value: object) -> dict[str, Any]:
    """Validate explicit organizer rules without supplying unobserved KNKV defaults.

    Raises:
        CupError: If a complete, bounded knockout rule set was not supplied.

    """
    if not isinstance(value, dict) or set(value) != {
        "regular_minutes",
        "extra_minutes",
        "shootout_attempts",
    }:
        raise CupError("Geef speelhelften, verlenging en het aantal strafworpen op.")
    for key in ("regular_minutes", "extra_minutes"):
        periods = value[key]
        if (
            not isinstance(periods, list)
            or len(periods) > MAX_PERIODS
            or any(
                type(minutes) is not int or not 1 <= minutes <= MAX_PERIOD_MINUTES
                for minutes in periods
            )
        ):
            raise CupError("Gebruik maximaal vier periodes van 1 tot 120 minuten.")
    if (
        not value["regular_minutes"]
        or sum(value["regular_minutes"]) > MAX_REGULAR_MINUTES
    ):
        raise CupError("Geef een reguliere speeltijd van maximaal 240 minuten op.")
    if (
        type(value["shootout_attempts"]) is not int
        or not 1 <= value["shootout_attempts"] <= MAX_SHOOTOUT_ATTEMPTS
    ):
        raise CupError(
            "Kies 1 tot 20 strafworpen per team, daarna om en om tot een beslissing."
        )
    return deepcopy(value)


def cup_state(match: TournamentMatch) -> dict[str, Any] | None:
    """Read cup progress; reads never initialize persistent state."""
    if not match.tournament.cup_rules:
        return None
    return deepcopy(match.cup_state) or {
        "rules": deepcopy(match.tournament.cup_rules),
        "phase": "regular",
        "period": 0,
        "completed_periods": [],
        "attempts": [],
        "shootout_first": None,
    }


def shootout_winner(state: dict[str, Any]) -> str | None:
    """Evaluate early clinching and equal-attempt sudden death separately from goals."""
    attempts = state["attempts"]
    taken = {
        side: sum(a["side"] == side for a in attempts) for side in ("home", "away")
    }
    scores = {
        side: sum(a["side"] == side and a["scored"] for a in attempts) for side in taken
    }
    count = state["rules"]["shootout_attempts"]
    for side, other in (("home", "away"), ("away", "home")):
        if max(taken.values()) <= count and scores[side] > scores[other] + max(
            0, count - taken[other]
        ):
            return side
        if (
            min(taken.values()) >= count
            and taken[side] == taken[other]
            and scores[side] > scores[other]
        ):
            return side
    return None


def _finish(match: TournamentMatch, side: str) -> None:
    winner = match.home_team if side == "home" else match.away_team
    if match.next_match_id:
        match.next_match = TournamentMatch.objects.select_for_update(of=("self",)).get(
            pk=match.next_match_id
        )
    if downstream_result_locked(match, winner):
        raise CupError(
            "Zet eerst de volgende wedstrijd terug voordat je deze winnaar wijzigt."
        )
    match.winner = winner
    match.status = TournamentMatch.Status.FINAL


def _advance(match: TournamentMatch, state: dict[str, Any]) -> None:
    if state["phase"] == "shootout":
        side = shootout_winner(state)
        if side is None:
            raise CupError("De strafworpserie heeft nog geen winnaar.")
        _finish(match, side)
        return
    key = "regular_minutes" if state["phase"] == "regular" else "extra_minutes"
    last_period = state["period"] + 1 == len(state["rules"][key])
    if last_period and match.home_score != match.away_score:
        _finish(
            match,
            "home" if (match.home_score or 0) > (match.away_score or 0) else "away",
        )
    state["completed_periods"].append({
        "phase": state["phase"],
        "period": state["period"],
        "home_score": match.home_score,
        "away_score": match.away_score,
    })
    if match.status == TournamentMatch.Status.FINAL:
        return
    if not last_period:
        state["period"] += 1
    elif state["phase"] == "regular" and state["rules"]["extra_minutes"]:
        state["phase"], state["period"] = "extra", 0
    else:
        state["phase"], state["period"] = "shootout", 0


def _penalty(state: dict[str, Any], side: str | None, scored: bool | None) -> None:
    if (
        state["phase"] != "shootout"
        or side not in {"home", "away"}
        or type(scored) is not bool
    ):
        raise CupError("Kies een team en raak of mis tijdens de strafworpserie.")
    if shootout_winner(state):
        raise CupError("De serie is beslist. Rond af of herstel de laatste strafworp.")
    first = state["shootout_first"] or side
    expected = (
        first
        if len(state["attempts"]) % 2 == 0
        else ("away" if first == "home" else "home")
    )
    if side != expected:
        raise CupError("Het andere team is aan de beurt.")
    state["shootout_first"] = first
    state["attempts"].append({"side": side, "scored": scored})


def apply_cup_command(
    match: TournamentMatch,
    *,
    payload: dict[str, Any],
    actor: object | None,
    actor_name: str,
) -> None:
    """Apply one revision-checked phase or penalty operation.

    The API owns the transaction, aggregate lock, revision check and publication.

    Raises:
        CupError: If the command is inconsistent with current play.

    """
    command = payload["command"]
    state = cup_state(match)
    if (
        state is None
        or state["phase"] == "unobserved"
        or match.status != TournamentMatch.Status.LIVE
        or not match.field_ready_at
        or not all((match.home_team_id, match.away_team_id))
    ):
        raise CupError(
            "Start eerst een bekerwedstrijd met twee teams en een gereed veld."
        )
    previous = deepcopy(state)
    previous_status = match.status
    if command == "advance":
        _advance(match, state)
    elif command == "penalty":
        _penalty(state, payload.get("side"), payload.get("scored"))
    elif command == "undo_penalty":
        if state["phase"] != "shootout" or not state["attempts"]:
            raise CupError("Er is geen strafworp om te herstellen.")
        state["attempts"].pop()
        if not state["attempts"]:
            state["shootout_first"] = None
    else:
        raise CupError("Onbekende bekeractie.")
    TournamentResultAudit.objects.create(
        match=match,
        previous_cup_state=previous,
        new_cup_state=state,
        previous_home_score=match.home_score,
        previous_away_score=match.away_score,
        new_home_score=match.home_score,
        new_away_score=match.away_score,
        previous_status=previous_status,
        new_status=match.status,
        reason=f"Cup {command}",
        changed_by=actor,
        changed_by_name=actor_name,
        source=TournamentResultAudit.Source.DIRECT,
    )
    match.cup_state = state
    match.revision += 1
    match.save(
        update_fields=["cup_state", "status", "winner", "revision", "updated_at"]
    )
    if match.status == TournamentMatch.Status.FINAL:
        sync_advanced_winner(match)


def start_cup_match(match: TournamentMatch, actor: object) -> None:
    """Start one cup fixture after the field's preceding match has finished.

    Raises:
        CupError: If this is not a cup or the selected field is occupied.

    """
    if not match.tournament.cup_rules or not match.field_id:
        raise CupError("Kies een veld voor deze bekerwedstrijd.")
    if (
        match.tournament.matches
        .filter(field_id=match.field_id, status=TournamentMatch.Status.LIVE)
        .exclude(pk=match.pk)
        .exists()
    ):
        raise CupError("Op dit veld is nog een wedstrijd bezig.")
    start_round([match], actor=actor)
