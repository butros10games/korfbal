"""Account-scoped private form jobs and native tracker integration."""

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from django.utils import timezone

from apps.competition.application.match_forms import (
    MatchFormError,
    MatchFormOptions,
    MatchFormProvider,
)
from apps.competition.models import (
    Match as SourceMatch,
    MatchFormAccess,
    MatchFormSync,
)
from apps.competition.services.match_form_payloads import (
    SUBSTITUTION_EVENT,
    event_signature,
    is_player,
    merge_substitutions,
    player_rows,
    publish_players,
    rows_at,
    selection_signature,
)
from apps.competition.services.rosters import parse_people, withdraw_people
from apps.game_tracker.application.ports import MatchChangePublisher
from apps.game_tracker.models import MatchData, MatchPart, PlayerGroup
from apps.game_tracker.services.live_update_signal_control import (
    suppress_live_update_signals,
)
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.match_events import active_match_events
from apps.game_tracker.services.match_mutations import (
    locked_match_mutation,
    require_match_revision,
)
from apps.game_tracker.services.player_designation import (
    can_edit_player_groups,
    sync_match_players_for_team,
)
from apps.game_tracker.services.player_groups import get_reserve_group
from apps.player.models import Player


MAX_PLAYERS = 16

ACTIONS = {"import", "publish", "substitutions"}


def resolve_scope(
    access: MatchFormAccess, match_id: object
) -> tuple[SourceMatch, MatchData, bool]:
    """Require native permissions and exact provider/native side bindings.

    Raises:
        MatchFormError: The account lacks access or the provider team is not linked.

    """
    source = (
        SourceMatch.objects
        .select_related(
            "local_match__home_team",
            "local_match__away_team",
            "pool__competition_class",
            "home_team__local_team_data",
            "away_team__local_team_data",
        )
        .filter(local_match_id=match_id)
        .first()
    )
    if (
        not access.enabled
        or not access.user.is_active
        or source is None
        or source.local_match is None
    ):
        raise MatchFormError("not_connected")
    match = source.local_match
    if access.team_id not in {
        match.home_team_id,
        match.away_team_id,
    } or not can_edit_player_groups(user=access.user, match=match, team=access.team):
        raise MatchFormError("access_denied")
    home = access.team_id == match.home_team_id
    side = source.home_team if home else source.away_team
    if side.local_team_data is None or side.local_team_data.team_id != access.team_id:
        raise MatchFormError("team_not_linked")
    return source, MatchData.objects.get(match_link=match), home


def allows_substitutions(access: MatchFormAccess, source: SourceMatch) -> bool:
    """Opt in the exact team and require a classified A-category poule."""
    return bool(
        access.auto_substitutions
        and source.pool
        and source.pool.competition_class
        and source.pool.competition_class.category == "a"
    )


def enqueue(
    access: MatchFormAccess,
    match_id: object,
    action: str,
    expected_revision: int,
    *,
    options: MatchFormOptions | None = None,
) -> MatchFormSync:
    """Queue once under the aggregate lock, preserving publication receipts.

    Raises:
        MatchFormError: The action is not allowed for this match or captain.

    """
    options = options or MatchFormOptions()
    captain_player_id = options.captain_player_id
    if action not in ACTIONS:
        raise MatchFormError("invalid_action")
    source, tracker, _ = resolve_scope(access, match_id)
    with locked_match_mutation(tracker.pk) as locked:
        require_match_revision(locked, expected_revision=expected_revision)
        if action in {"import", "publish"} and locked.status != "upcoming":
            raise MatchFormError("match_started")
        if action == "substitutions" and (
            locked.status != "finished" or not allows_substitutions(access, source)
        ):
            raise MatchFormError("substitutions_not_enabled")
        if action == "publish":
            _captain_person_id(locked, access, captain_player_id)
        elif captain_player_id is not None:
            raise MatchFormError("invalid_action")
        job = MatchFormSync.objects.filter(
            access=access,
            match_id=match_id,
            action=action,
        ).first()
        if job and (
            job.state in {"pending", "running"}
            or (
                options.automatic
                and action == "substitutions"
                and job.expected_revision == expected_revision
            )
        ):
            return job
        if (
            options.automatic
            and action == "import"
            and not import_is_due(source.starts_at, timezone.now(), job)
        ):
            if job is None:
                raise MatchFormError("import_not_due")
            return job
        if (
            job
            and action == "publish"
            and (
                job.expected_revision != expected_revision
                or job.captain_player_id != captain_player_id
            )
        ):
            job.publication_intent = {}
        job = job or MatchFormSync(access=access, match_id=match_id, action=action)
        job.automatic = options.automatic
        job.state = "pending"
        job.error_code = ""
        job.attempts = 0
        job.expected_revision = expected_revision
        job.captain_player_id = captain_player_id
        job.next_attempt_at = job.updated_at = timezone.now()
        job.save()
        return job


def import_reserves(
    job: MatchFormSync,
    scope: tuple[SourceMatch, MatchData, bool],
    form: dict,
    publisher: MatchChangePublisher,
) -> None:
    """Add visible selected players to the bank without replacing existing groups.

    Raises:
        MatchFormError: The match started, the selection is too large, or a player
            is already assigned to the opposing team.

    """
    source, tracker, home = scope
    form_rows = player_rows(form, home, editing=False)
    rows = [
        row for row in form_rows if is_player(row) and row.get("OnMatchForm") is True
    ]
    visible, hidden = parse_people([{**row, "TeamPerson": True} for row in form_rows])
    selected_ids = {row["PersonId"] for row in rows} - hidden
    visible = {key: value for key, value in visible.items() if key in selected_ids}
    if len(visible) > MAX_PLAYERS:
        raise MatchFormError("too_many_players")
    with locked_match_mutation(tracker.pk) as locked, suppress_live_update_signals():
        require_match_revision(locked, expected_revision=job.expected_revision)
        if locked.status != "upcoming":
            raise MatchFormError("match_started")
        observed_at = timezone.now()
        withdrawn = Player.all_objects.filter(
            knkv_person_id__in=hidden, knkv_observed_at__lte=observed_at
        ).exists()
        withdraw_people(hidden, source.season, observed_at)
        reserve = get_reserve_group(match_data=locked, team=job.access.team)
        current = Player.all_objects.filter(player_groups__match_data=locked).distinct()
        own_ids = set(
            current.filter(player_groups__team=job.access.team).values_list(
                "pk", flat=True
            )
        )
        other_ids = set(current.exclude(pk__in=own_ids).values_list("pk", flat=True))
        added = 0
        available = 0
        for person_id, (name, _shirt, privacy, _roles) in visible.items():
            if person_id == "PRIVATE":
                continue
            player, _ = Player.all_objects.get_or_create(
                knkv_person_id=person_id,
                defaults={
                    "name": name,
                    "knkv_privacy": privacy,
                    "knkv_observed_at": timezone.now(),
                },
            )
            if player.archived_at is not None or player.knkv_privacy == "PRIVATE":
                continue
            available += 1
            player.knkv_privacy = privacy
            player.knkv_observed_at = timezone.now()
            player.save(update_fields=["knkv_privacy", "knkv_observed_at"])
            if player.pk in other_ids:
                raise MatchFormError("player_on_other_team")
            if player.pk not in own_ids:
                if reserve.players.count() >= MAX_PLAYERS:
                    raise MatchFormError("too_many_players")
                reserve.players.add(player)
                added += 1
        if added or withdrawn:
            sync_match_players_for_team(match_data=locked, team=job.access.team)
            record_match_change(locked, publisher=publisher)
        job.captain_player = _imported_captain(rows, locked, job.access)
        job.player_count = available
        job.state = "succeeded"
        job.updated_at = timezone.now()
        job.save(
            update_fields=["player_count", "captain_player", "state", "updated_at"]
        )


def _imported_captain(
    rows: list[dict], tracker: MatchData, access: MatchFormAccess
) -> Player | None:
    """Remember one visible imported captain using the native player identity."""
    captains = {row.get("PersonId") for row in rows if row.get("Captain") is True}
    if len(captains) != 1:
        return None
    return (
        Player.all_objects
        .filter(
            knkv_person_id__in=captains,
            player_groups__match_data=tracker,
            player_groups__team=access.team,
            archived_at__isnull=True,
        )
        .exclude(knkv_privacy="PRIVATE")
        .first()
    )


def _captain_person_id(
    tracker: MatchData, access: MatchFormAccess, player_id: UUID | None
) -> str:
    """Require a linked, available captain in this team's current match selection.

    Raises:
        MatchFormError: No eligible selected captain was supplied.

    """
    if player_id is None:
        raise MatchFormError("captain_required")
    player = Player.all_objects.filter(
        pk=player_id,
        player_groups__match_data=tracker,
        player_groups__team=access.team,
        archived_at__isnull=True,
    ).first()
    if (
        player is None
        or not player.knkv_person_id
        or player.knkv_person_id == "PRIVATE"
        or player.knkv_privacy == "PRIVATE"
    ):
        raise MatchFormError("captain_not_selected")
    return player.knkv_person_id


def _selection(tracker: MatchData, access: MatchFormAccess) -> dict[str, bool]:
    selected = {}
    players = PlayerGroup.objects.filter(
        match_data=tracker, team=access.team, players__isnull=False
    ).values_list("players__knkv_person_id", "starting_type__name")
    for person_id, group_name in players:
        if not person_id or person_id == "PRIVATE" or person_id in selected:
            raise MatchFormError("players_not_linked")
        selected[person_id] = group_name != "Reserve"
    return selected


def _substitutions(
    tracker: MatchData,
    access: MatchFormAccess,
    source: SourceMatch,
    home: bool,
    details: dict,
) -> list[dict[str, Any]]:
    periods = [
        p
        for p in rows_at(details, "MatchPeriod")
        if p.get("IsPlayPeriod") is True and p.get("IsPenaltyTime") is not True
    ]
    if len(periods) < tracker.parts or any(
        type(p.get("PeriodId")) is not int or type(p.get("PlayTime")) is not int
        for p in periods
    ):
        raise MatchFormError("timing_not_supported")
    if any(p["PlayTime"] * 60 != tracker.part_length for p in periods[: tracker.parts]):
        raise MatchFormError("timing_not_supported")
    resolution = details.get("EventTimeResolution")
    if resolution not in {"NONE", "MINUTE"}:
        raise MatchFormError("timing_not_supported")
    parts = {
        str(p.pk): p.part_number for p in MatchPart.objects.filter(match_data=tracker)
    }
    events = (
        active_match_events(tracker, source_types={"player_change"})
        .filter(substitution_detail__team=access.team)
        .select_related(
            "substitution_detail__player_in", "substitution_detail__player_out"
        )
    )
    result = []
    for event in events:
        change = event.substitution_detail
        if (
            change.player_in is None
            or change.player_out is None
            or not change.player_in.knkv_person_id
            or not change.player_out.knkv_person_id
        ):
            raise MatchFormError("players_not_linked")
        number = parts.get(str(event.period_id))
        if (
            number is None
            or not 1 <= number <= tracker.parts
            or event.elapsed_ms is None
        ):
            raise MatchFormError("timing_not_supported")
        minute = event.elapsed_ms // 60000
        result.append({
            "PublicMatchId": source.external_id,
            "PublicTeamId": (
                source.home_team if home else source.away_team
            ).external_id,
            "ClientEventId": str(
                uuid5(
                    NAMESPACE_URL,
                    f"korfbal:knkv:{source.external_id}:{event.logical_id}",
                )
            ),
            "TypeOfEvent": SUBSTITUTION_EVENT,
            "PersonId": change.player_out.knkv_person_id,
            "OtherPersonId": change.player_in.knkv_person_id,
            "RoleId": "PLAYER_DEFAULT",
            "PeriodId": periods[number - 1]["PeriodId"],
            "OffsetTime": str(minute),
            "MatchEventDetails": [],
        })
    return result


def execute(
    job: MatchFormSync, provider: MatchFormProvider, publisher: MatchChangePublisher
) -> None:
    """Fetch authorized forms, take a current local snapshot, publish and verify.

    Raises:
        MatchFormError: The form is inaccessible, invalid, or cannot be confirmed.

    """
    source, tracker, home = resolve_scope(job.access, job.match_id)
    if job.action == "import":
        if job.automatic and not (
            source.starts_at - timedelta(hours=1) <= timezone.now() < source.starts_at
        ):
            raise MatchFormError("import_not_due")
        form = provider.read("players", source.external_id, home=home)
        import_reserves(job, (source, tracker, home), form, publisher)
        return
    if job.action == "publish":
        _publish_selection(job, provider, source, tracker, home)
        return
    if not allows_substitutions(job.access, source):
        raise MatchFormError("substitutions_not_enabled")
    form = provider.read("events", source.external_id)
    details = provider.read("details", source.external_id)
    with locked_match_mutation(tracker.pk) as locked:
        if locked.status != "finished":
            raise MatchFormError("match_not_finished")
        # Snapshot canonical, corrected facts; recomputation may have advanced revision.
        desired = _substitutions(locked, job.access, source, home, details)
        job.expected_revision = locked.live_revision
    team_id = (source.home_team if home else source.away_team).external_id
    updated = merge_substitutions(
        form,
        home=home,
        team_id=team_id,
        desired=desired,
        owned_ids=job.published_event_ids,
    )
    # Persist ownership intent before I/O: a timeout may still have committed the PUT.
    job.published_event_ids = sorted(
        set(job.published_event_ids) | {row["ClientEventId"] for row in desired}
    )
    job.save(update_fields=["published_event_ids", "expected_revision"])
    result = provider.replace("events", source.external_id, form, updated)
    actual = {
        row.get("ClientEventId"): row
        for row in rows_at(result, "MatchFormMatchEvents", "MatchEvent")
    }
    if any(
        event_signature(actual.get(row["ClientEventId"], {})) != event_signature(row)
        for row in desired
    ):
        raise MatchFormError("publication_not_confirmed")
    ids = [row["ClientEventId"] for row in desired]
    if any(old in actual for old in set(job.published_event_ids) - set(ids)):
        raise MatchFormError("publication_not_confirmed")
    job.published_event_ids = ids
    job.event_count = len(desired)


def _publish_selection(
    job: MatchFormSync,
    provider: MatchFormProvider,
    source: SourceMatch,
    tracker: MatchData,
    home: bool,
) -> None:
    """Publish only a revision-checked pre-match selection.

    Raises:
        MatchFormError: The match started or KNKV rejected the selection or captain.

    """
    form = provider.read("players", source.external_id, home=home)
    intent = job.publication_intent
    if intent and form.get("InputForm", {}).get("CaptainApproved") is True:
        actual = selection_signature(form, home, allows_base=intent["allows_base"])
        if _selection_digest(actual) != intent["digest"]:
            raise MatchFormError("knkv_changed")
        job.player_count = len(actual)
        return
    info = provider.read("info", source.external_id)
    allows_base = (
        info.get("Details", {}).get("ClassAttributes", {}).get("AllowsBasePlayers")
    )
    if type(allows_base) is not bool:
        raise MatchFormError("invalid_response")
    with locked_match_mutation(tracker.pk) as locked:
        require_match_revision(locked, expected_revision=job.expected_revision)
        if locked.status != "upcoming":
            raise MatchFormError("match_started")
        selected = _selection(locked, job.access)
        captain_id = _captain_person_id(locked, job.access, job.captain_player_id)
    draft = deepcopy(form)
    rows = player_rows(draft, home)
    known = {row.get("PersonId") for row in rows if is_player(row)}
    for person_id in selected.keys() - known:
        rows.append(provider.find_player(source.external_id, home, person_id))
    updated = publish_players(
        draft, home, selected, allows_base=allows_base, captain_id=captain_id
    )
    expected = selection_signature(updated, home, allows_base=allows_base)
    job.publication_intent = {
        "allows_base": allows_base,
        "digest": _selection_digest(expected),
    }
    job.save(update_fields=["publication_intent"])
    result = provider.replace("players", source.external_id, form, updated, home=home)
    if (
        selection_signature(result, home, allows_base=allows_base) != expected
        or result.get("InputForm", {}).get("CaptainApproved") is not True
    ):
        raise MatchFormError("publication_not_confirmed")
    job.player_count = len(selected)


def _selection_digest(signature: set[tuple]) -> str:
    """Persist a publication fingerprint, never the private provider form."""
    return sha256(
        json.dumps(sorted(signature), separators=(",", ":")).encode()
    ).hexdigest()


def import_slots(starts_at: datetime) -> tuple[datetime, ...]:
    """Shared schedule for discovery and abandoned-job recovery, before kickoff."""
    return tuple(
        starts_at - timedelta(minutes=minutes)
        for minutes in (60, 30, 25, 20, 15, 10, 5)
    )


def import_is_due(
    starts_at: datetime, now: datetime, job: MatchFormSync | None
) -> bool:
    """Attempt at minus 60, minus 30, then each five minutes until scheduled start."""
    slots = import_slots(starts_at)
    first = slots[0]
    if not first <= now < starts_at:
        return False
    if job is None:
        return True
    if job.state in {"pending", "running"}:
        return False
    if job.state == "succeeded" and job.player_count > 0 and job.updated_at >= first:
        return False
    return any(job.updated_at < slot <= now for slot in slots)
