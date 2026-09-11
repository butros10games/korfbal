"""Preserve KNKV-owned form fields while changing only our team's selection/events."""

from copy import deepcopy
from typing import Any

from apps.competition.application.match_forms import MatchFormError


SUBSTITUTION_EVENT = 10


def rows_at(form: dict, *path: str) -> list[dict[str, Any]]:
    """Require a complete collection; malformed input must never clear a form.

    Raises:
        MatchFormError: A collection is missing or contains malformed rows.

    """
    value = form
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise MatchFormError("invalid_response")
        value = value[key]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise MatchFormError("invalid_response")
    return value


def player_rows(form: dict, home: bool) -> list[dict]:
    """Require both the requested side and the provider's edit permission.

    Raises:
        MatchFormError: The form belongs to the wrong side or does not allow editing.

    """
    if form.get("IsHome") is not home:
        raise MatchFormError("invalid_response")
    if form.get("Permissions", {}).get("TeamEditAllowed") is not True:
        raise MatchFormError("knkv_access_denied")
    return rows_at(form, "MatchFormTeamPersons", "MatchFormTeamPerson")


def is_player(row: dict) -> bool:
    """Separate player roles from staff, even when they share a person identity."""
    return (row.get("TeamPersonFunction") or {}).get("RoleId") == "PLAYER_DEFAULT"


def publish_players(
    form: dict,
    home: bool,
    selected: dict[str, bool],
    *,
    allows_base: bool,
    captain_id: str,
) -> dict:
    """Publish selected players without assigning KNKV-unsupported court positions.

    Raises:
        MatchFormError: The selection or captain is invalid, or editing is not allowed.

    """
    updated = deepcopy(form)
    rows = player_rows(updated, home)
    known = {row.get("PersonId") for row in rows if is_player(row)}
    if not selected or not set(selected) <= known or "PRIVATE" in selected:
        raise MatchFormError("players_not_linked")
    if captain_id not in selected:
        raise MatchFormError("captain_not_selected")
    if allows_base and not selected[captain_id]:
        raise MatchFormError("captain_must_start")
    for row in rows:
        if is_player(row):
            row["Captain"] = row.get("PersonId") == captain_id
            row["OnMatchForm"] = row.get("PersonId") in selected
            if allows_base:
                row["BasePlayer"] = selected.get(row.get("PersonId"), False)
    inputs = updated.get("InputForm")
    if not isinstance(inputs, dict):
        raise MatchFormError("knkv_access_denied")
    inputs["CaptainApproved"] = True
    inputs["OverrideWarnings"] = False
    # CaptainApproved submits the team to the official; TeamLocked is official approval.
    # Preserve official approval, identification, staff, shirts and formation.
    return updated


def selection_signature(form: dict, home: bool, *, allows_base: bool) -> set[tuple]:
    """Compare the published selection independently of server-added metadata."""
    return {
        (
            row.get("PersonId"),
            row.get("BasePlayer") if allows_base else None,
            row.get("Captain"),
        )
        for row in rows_at(form, "MatchFormTeamPersons", "MatchFormTeamPerson")
        if is_player(row)
        and row.get("OnMatchForm") is True
        and form.get("IsHome") is home
    }


def event_signature(event: dict) -> tuple:
    """Compare substitution meaning independently of server event identifiers."""
    return tuple(
        event.get(key)
        for key in (
            "PublicTeamId",
            "TypeOfEvent",
            "PersonId",
            "OtherPersonId",
            "PeriodId",
            "OffsetTime",
        )
    )


def merge_substitutions(
    form: dict, *, home: bool, team_id: str, desired: list[dict], owned_ids: list[str]
) -> dict:
    """Keep opponent events and manual entries; replace only acknowledged own IDs.

    Raises:
        MatchFormError: Editing is denied or an event conflicts with a KNKV entry.

    """
    permission = (
        "MatchEventEditHomeTeamAllowed" if home else "MatchEventEditAwayTeamAllowed"
    )
    if form.get("Permissions", {}).get(permission) is not True:
        raise MatchFormError("knkv_access_denied")
    updated = deepcopy(form)
    existing = rows_at(updated, "MatchFormMatchEvents", "MatchEvent")
    desired_by_id = {event["ClientEventId"]: event for event in desired}
    ours = set(owned_ids) | set(desired_by_id)
    result = []
    seen = set()
    manual_signatures = set()
    for event in existing:
        client_id = event.get("ClientEventId")
        if client_id in ours:
            if (
                event.get("PublicTeamId") != team_id
                or event.get("TypeOfEvent") != SUBSTITUTION_EVENT
                or client_id in seen
            ):
                raise MatchFormError("knkv_changed")
            seen.add(client_id)
            if client_id in desired_by_id:
                result.append({**event, **desired_by_id[client_id]})
        else:
            result.append(event)
            manual_signatures.add(event_signature(event))
    for client_id, event in desired_by_id.items():
        if client_id not in seen:
            if event_signature(event) in manual_signatures:
                # Do not silently adopt an independently entered substitution.
                raise MatchFormError("manual_substitution_conflict")
            result.append(event)
    updated["MatchFormMatchEvents"]["MatchEvent"] = result
    return updated
