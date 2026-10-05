"""Minimal visible roster snapshots and dated memberships from one team feed."""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.domain.rosters import (
    ROSTER_FRESHNESS,
    RosterPayloadError,
    RosterPeriod,
    roster_target,
)
from apps.competition.models import (
    MatchMembership,
    RosterMembership,
    SyncResource,
    Team,
    TeamParticipation,
)
from apps.competition.services.player_photos import discover_photo
from apps.competition.services.seasons import SeasonResolver
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import TeamData
from apps.team.services.roster_history import reconcile_roster_history


SOURCE_ID_LIMIT = 80
NAME_LIMIT = 255
SHIRT_LIMIT = 10
PRIVACY_LIMIT = 16
MAX_DISCOVERY_BATCH = 1000
VISIBLE_LEVELS = {"OPEN", "NORMAL", "LIMITED"}


@transaction.atomic
def import_roster(
    season: Season, source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Replace a complete visible roster atomically and erase withdrawn identities.

    Raises:
        RosterPayloadError: A malformed envelope cannot retire an existing roster.

    """
    team = Team.objects.select_for_update(no_key=True).get(
        season=season, external_id=source_id
    )
    rows = data.get("TeamPersonOverview")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RosterPayloadError("collection")
    if team.roster_observed_at and team.roster_observed_at > observed_at:
        return
    visible, hidden = parse_people(rows)
    withdraw_people(hidden, season, observed_at)
    current = []
    for person_id, (name, shirt, privacy, roles) in visible.items():
        if person_id in hidden:
            continue
        player, _ = Player.all_objects.select_for_update(no_key=True).get_or_create(
            knkv_person_id=person_id,
            defaults={"name": name, "knkv_observed_at": observed_at},
        )
        if player.archived_at is not None:
            continue
        if player.knkv_observed_at and player.knkv_observed_at > observed_at:
            # Identity freshness and team membership are independent observations.
            # An older feed can retain a present member, but cannot undo withdrawal.
            if player.knkv_privacy not in VISIBLE_LEVELS:
                continue
        else:
            Player.all_objects.filter(pk=player.pk).update(
                knkv_observed_at=observed_at,
                knkv_privacy=privacy,
                **({"name": name} if player.user_id is None else {}),
            )
        membership, _ = RosterMembership.objects.get_or_create(
            player=player,
            team=team,
            ended_at=None,
            defaults={"first_seen_at": observed_at, "last_seen_at": observed_at},
        )
        RosterMembership.objects.filter(pk=membership.pk).update(
            last_seen_at=max(membership.last_seen_at, observed_at),
            shirt_number=shirt,
            roles=roles,
        )
        current.append(membership.pk)
    RosterMembership.objects.filter(team=team, ended_at=None).exclude(
        pk__in=current
    ).update(ended_at=observed_at)
    _discover_photos(rows, set(visible) - hidden, season, observed_at)
    team.private_roster_counts = count_private_people(rows, hidden)
    team.roster_observed_at = observed_at
    team.save(update_fields=("private_roster_counts", "roster_observed_at"))
    publish_roster(team)


@transaction.atomic
def withdraw_people(hidden: set[str], season: Season, observed_at: datetime) -> None:
    """Erase provider observations when a newer response withdraws visibility."""
    withdrawn = Player.all_objects.filter(
        knkv_person_id__in=hidden, knkv_observed_at__lte=observed_at
    )
    for player in withdrawn:
        discover_photo(player, None, season)
    source_only = withdrawn.filter(user_id=None)
    removals = {}
    affected_ids = set()
    for relation, flag in ROSTER_RELATIONS.items():
        through = getattr(TeamData, relation).through
        owned_links = (
            RosterMembership.objects
            .filter(player_id__in=withdrawn.values("pk"), **{flag: True})
            .exclude(published_team_data=None)
            .values_list("player_id", "published_team_data_id")
        )
        remove_links = Q(player_id__in=source_only.values("pk"))
        for player_id, team_data_id in owned_links:
            remove_links |= Q(player_id=player_id, teamdata_id=team_data_id)
        removals[relation] = remove_links
        affected_ids.update(
            through.objects.filter(remove_links).values_list("teamdata_id", flat=True)
        )
    affected_teams = list(
        TeamData.objects.select_for_update().filter(pk__in=affected_ids).order_by("pk")
    )
    for relation, remove_links in removals.items():
        through = getattr(TeamData, relation).through
        through.objects.filter(remove_links).delete()
        for team_data in affected_teams:
            reconcile_roster_history(team_data=team_data, role=relation, source="knkv")
    RosterMembership.objects.filter(player_id__in=withdrawn.values("pk")).delete()
    MatchMembership.objects.filter(player_id__in=withdrawn.values("pk")).delete()
    source_only.update(name="", knkv_privacy="PRIVATE", knkv_observed_at=observed_at)
    withdrawn.exclude(user_id=None).update(
        knkv_privacy="PRIVATE", knkv_observed_at=observed_at
    )


def _person_contract(row: dict[str, Any]) -> tuple[str, str, str]:
    """Validate identity, privacy and role even when the row is hidden.

    Raises:
        RosterPayloadError: The minimal membership contract is malformed.

    """
    person_id = row.get("PersonId")
    if (
        not isinstance(person_id, str)
        or not person_id
        or len(person_id) > SOURCE_ID_LIMIT
    ):
        raise RosterPayloadError("identity")
    privacy = row.get("PrivacyLevel")
    if not isinstance(privacy, str) or not privacy or len(privacy) > PRIVACY_LIMIT:
        raise RosterPayloadError("privacy")
    if not isinstance(row.get("TeamPerson"), bool):
        raise RosterPayloadError("membership")
    role = row.get("TeamPersonFunction")
    if not isinstance(role, dict):
        raise RosterPayloadError("role")
    role_id = role.get("RoleId")
    if not isinstance(role_id, str) or not role_id or len(role_id) > SOURCE_ID_LIMIT:
        raise RosterPayloadError("role")
    return person_id, privacy, role_id


def parse_people(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, str, str, list[str]]], set[str]]:
    """Whitelist visible players and minimal fields before applying any writes.

    Raises:
        RosterPayloadError: Required player fields are malformed.

    """
    visible: dict[str, tuple[str, str, str, list[str]]] = {}
    hidden = set()
    for row in rows:
        person_id, privacy, role_id = _person_contract(row)
        if privacy not in VISIBLE_LEVELS:
            hidden.add(person_id)
            continue
        if row.get("TeamPerson") is not True or role_id not in {
            "PLAYER_DEFAULT",
            "COACHING_STAFF",
            "MEDICAL_STAFF",
            "OTHER_STAFF",
        }:
            continue
        parts = [row.get(key) for key in ("FirstName", "Infix", "LastName")]
        if any(part is not None and not isinstance(part, str) for part in parts):
            raise RosterPayloadError("name")
        parts = [part or "" for part in parts]
        name = " ".join(part.strip() for part in parts if part.strip())
        if not name or len(name) > NAME_LIMIT:
            raise RosterPayloadError("name")
        shirt_value = row.get("ShirtNumber")
        if shirt_value is not None and (
            not isinstance(shirt_value, (int, str)) or isinstance(shirt_value, bool)
        ):
            raise RosterPayloadError("shirt")
        shirt = str(shirt_value) if shirt_value is not None else ""
        if len(shirt) > SHIRT_LIMIT:
            raise RosterPayloadError("shirt")
        previous_roles = visible[person_id][3] if person_id in visible else []
        visible[person_id] = (
            name,
            shirt,
            privacy,
            sorted({*previous_roles, role_id}),
        )
    return visible, hidden


@dataclass(frozen=True, slots=True)
class RosterQueueSelection:
    """Scope a discovery page by public variant IDs, sport and stable cursor."""

    sport: str | None = None
    source_ids: tuple[str, ...] = ()
    after: str = ""


@dataclass(frozen=True, slots=True)
class RosterQueuePlan:
    """A bounded, local-only selection of missing feeds and explicit refreshes."""

    season: Season
    missing: tuple[str, ...]
    refresh: tuple[str, ...]
    next_cursor: str | None
    counts: dict[str, int]

    def report(self) -> dict[str, object]:
        """Describe public source IDs and aggregate outcomes without person data."""
        return {
            "season": self.season.name,
            "counts": self.counts,
            "selected_missing": list(self.missing),
            "selected_private_refresh": list(self.refresh),
            "next_cursor": self.next_cursor,
            "selected_feeds": len(self.missing) + len(self.refresh),
        }


def plan_rosters(
    season: Season,
    *,
    refresh_private: bool = False,
    limit: int | None = 20,
    selection: RosterQueueSelection | None = None,
) -> RosterQueuePlan:
    """Preview distinct roster discovery without HTTP or database writes.

    Raises:
        ValueError: Live rosters cannot be assigned to historical seasons.
        ValueError: The requested selection exceeds the bounded command contract.

    """
    selection = selection or RosterQueueSelection()
    now = timezone.now()
    if not season.start_date <= timezone.localdate(now) <= season.end_date:
        raise ValueError("Roster feeds only support the current season")
    if limit is not None and not 1 <= limit <= MAX_DISCOVERY_BATCH:
        raise ValueError("Roster selection limit must be between 1 and 1000")
    requested = tuple(dict.fromkeys(selection.source_ids))
    if len(requested) > MAX_DISCOVERY_BATCH or any(
        not isinstance(source_id, str)
        or not source_id
        or len(source_id) > SOURCE_ID_LIMIT
        for source_id in requested
    ):
        raise ValueError("Roster source IDs must be bounded nonempty strings")
    if len(selection.after) > SOURCE_ID_LIMIT:
        raise ValueError("Roster cursor is too long")
    teams = Team.objects.filter(season=season)
    if selection.sport:
        teams = teams.filter(sport=selection.sport)
    if requested:
        teams = teams.filter(external_id__in=requested)
        if teams.count() != len(requested):
            raise ValueError("Every selected roster source must belong to the scope")
    feeds = SyncResource.objects.filter(season=season, kind="team_roster")
    missing = teams.exclude(external_id__in=feeds.values("source_id"))
    private = teams.filter(
        Q(private_roster_counts__players__gt=0) | Q(private_roster_counts__staff__gt=0)
    )
    successful_private = feeds.filter(
        source_id__in=private.values("external_id"),
        fetched_at__isnull=False,
        failures=0,
    )
    refresh = successful_private if refresh_private else feeds.none()
    candidates = teams.filter(
        Q(pk__in=missing.values("pk")) | Q(external_id__in=refresh.values("source_id")),
        external_id__gt=selection.after,
    ).order_by("external_id")
    selected = list(
        candidates.values_list("external_id", flat=True)[
            : None if limit is None else limit + 1
        ]
    )
    more = limit is not None and len(selected) > limit
    if more:
        selected = selected[:limit]
    missing_ids = set(
        missing.filter(external_id__in=selected).values_list("external_id", flat=True)
    )
    return RosterQueuePlan(
        season=season,
        missing=tuple(source_id for source_id in selected if source_id in missing_ids),
        refresh=tuple(
            source_id for source_id in selected if source_id not in missing_ids
        ),
        next_cursor=selected[-1] if more else None,
        counts={
            "known_variants": teams.count(),
            "missing_feeds": missing.count(),
            "unobserved_missing_feeds": missing.filter(roster_observed_at=None).count(),
            "observed_empty": teams
            .filter(
                roster_observed_at__isnull=False,
                private_roster_counts__players=0,
                private_roster_counts__staff=0,
            )
            .exclude(
                pk__in=RosterMembership.objects.filter(ended_at=None).values("team_id")
            )
            .count(),
            "private_feeds": private.count(),
            "successful_private_refresh": successful_private.count(),
            "selected": len(selected),
        },
    )


@transaction.atomic
def apply_roster_plan(plan: RosterQueuePlan) -> int:
    """Queue only selected feeds; preserve failed and already pending work.

    Raises:
        ValueError: The selected source season is no longer current.

    """
    now = timezone.now()
    if not plan.season.start_date <= timezone.localdate(now) <= plan.season.end_date:
        raise ValueError("Roster feeds only support the current season")
    feeds = SyncResource.objects.filter(
        season=plan.season, kind="team_roster", source_id__in=plan.missing
    )
    before = feeds.count()
    SyncResource.objects.bulk_create(
        [
            SyncResource(
                season=plan.season,
                kind="team_roster",
                source_id=source_id,
                next_sync_at=now,
            )
            for source_id in Team.objects.filter(
                season=plan.season, external_id__in=plan.missing
            ).values_list("external_id", flat=True)
        ],
        ignore_conflicts=True,
        batch_size=1000,
    )
    # A stale preview cannot reset a failure ceiling or duplicate pending work.
    affected = Team.objects.filter(
        season=plan.season, external_id__in=plan.refresh
    ).filter(
        Q(private_roster_counts__players__gt=0) | Q(private_roster_counts__staff__gt=0)
    )
    refreshed = SyncResource.objects.filter(
        season=plan.season,
        kind="team_roster",
        source_id__in=affected.values("external_id"),
        fetched_at__isnull=False,
        failures=0,
    ).update(fetched_at=None, etag="", next_sync_at=now)
    return feeds.count() - before + refreshed


def queue_rosters(season: Season, *, refresh_private: bool = False) -> int:
    """Preserve the existing service's explicit, idempotent full-scope queue API."""
    return apply_roster_plan(
        plan_rosters(season, refresh_private=refresh_private, limit=None)
    )


ROSTER_RELATIONS = {
    "players": "local_link_created",
    "staff": "local_staff_link_created",
    "coach": "local_coach_link_created",
}


def _belongs(observation: RosterMembership, relation: str) -> bool:
    roles = observation.roles or ["PLAYER_DEFAULT"]
    if relation == "players":
        return "PLAYER_DEFAULT" in roles
    if relation == "coach":
        return "COACHING_STAFF" in roles
    return any(role != "PLAYER_DEFAULT" for role in roles)


@transaction.atomic
def publish_roster(team: Team) -> None:
    """Reconcile importer-owned links independently for each native team season."""
    if team.group is None:
        return
    observations = list(
        RosterMembership.objects.filter(team__group=team.group).select_related("team")
    )
    resolver = SeasonResolver()
    fallback = (
        team.group.local_team_data_id if team.season_id not in resolver.scopes else None
    )
    target = participation_targets(team.group_id, fallback)
    # A finished competition period is history: its roster stays as published
    # (with its ownership) instead of being emptied when the next one starts.
    closed = set(
        TeamParticipation.objects.filter(
            team__group_id=team.group_id,
            team_data__season__end_date__lt=timezone.localdate(),
        ).values_list("team_data_id", flat=True)
    )
    frozen = {row.pk for row in observations if target(row) in closed}
    targets = {target(row) for row in observations} | {
        row.published_team_data_id for row in observations
    }
    targets -= closed
    targets.discard(None)
    visible = set(
        Player.objects.filter(
            pk__in=[row.player_id for row in observations]
        ).values_list("pk", flat=True)
    )
    new_ownership = {
        row.pk: dict.fromkeys(ROSTER_RELATIONS.values(), False) for row in observations
    }
    cutoff = timezone.now() - ROSTER_FRESHNESS
    for data in (
        TeamData.objects.select_for_update().filter(pk__in=targets).order_by("pk")
    ):
        desired_rows = [
            row
            for row in observations
            if target(row) == data.pk
            and row.ended_at is None
            and row.last_seen_at >= cutoff
            and row.player_id in visible
        ]
        for relation, flag in ROSTER_RELATIONS.items():
            wanted = {row.player_id for row in desired_rows if _belongs(row, relation)}
            owned = {
                row.player_id
                for row in observations
                if row.published_team_data_id == data.pk and getattr(row, flag)
            }
            through = getattr(TeamData, relation).through
            present = set(
                through.objects.filter(teamdata_id=data.pk).values_list(
                    "player_id", flat=True
                )
            )
            through.objects.filter(
                teamdata_id=data.pk, player_id__in=owned - wanted
            ).delete()
            through.objects.bulk_create(
                [through(teamdata_id=data.pk, player_id=pk) for pk in wanted - present],
                ignore_conflicts=True,
            )
            reconcile_roster_history(team_data=data, role=relation, source="knkv")
            for pk in wanted & (owned | (wanted - present)):
                owner = next(
                    row
                    for row in desired_rows
                    if row.player_id == pk and _belongs(row, relation)
                )
                new_ownership[owner.pk][flag] = True
    for row in observations:
        if row.pk in frozen:
            continue
        for flag, value in new_ownership[row.pk].items():
            setattr(row, flag, value)
        row.published_team_data_id = target(row)
    RosterMembership.objects.bulk_update(
        observations, ["published_team_data", *ROSTER_RELATIONS.values()]
    )


def participation_targets(
    group_id: int | None, fallback: int | None
) -> Callable[[RosterMembership], int | None]:
    """Return the native roster an observation belongs to on its observation day.

    A team entered in several competition periods publishes a roster seen on a
    given day only to the period running that day; outside every period it
    falls back to the team's default season. Observations never reach back into
    earlier periods, so current rosters are not projected onto history.
    """
    periods: dict[int, list[RosterPeriod]] = defaultdict(list)
    for participation in TeamParticipation.objects.filter(
        team__group_id=group_id
    ).select_related("team_data__season"):
        periods[participation.team_id].append(
            RosterPeriod(
                team_data_id=participation.team_data_id,
                start_date=participation.team_data.season.start_date,
                end_date=participation.team_data.season.end_date,
                phase=participation.phase,
                order=participation.pk,
            )
        )

    def target(row: RosterMembership) -> int | None:
        return roster_target(
            row.last_seen_at,
            periods.get(row.team_id, []),
            row.team.local_team_data_id or fallback,
        )

    return target


def publish_pending_rosters(extra_groups: Iterable[int] = ()) -> None:
    """Publish new or season-remapped observations once per global source group.

    ``extra_groups`` are groups whose teams gained a competition period.
    """
    groups = set(
        RosterMembership.objects.filter(published_team_data=None).values_list(
            "team__group_id", flat=True
        )
    ) | set(extra_groups)
    for group_id in groups:
        team = Team.objects.filter(
            group_id=group_id, group__local_team__isnull=False
        ).first()
        if team:
            publish_roster(team)


def _discover_photos(
    rows: list[dict[str, Any]],
    person_ids: set[str],
    season: Season,
    observed_at: datetime,
) -> None:
    """Reconcile photos after validated identity updates, strictest privacy wins."""
    # Reconcile photos only after all identity validation and privacy updates.
    for person_id in person_ids:
        player = Player.all_objects.select_for_update().get(knkv_person_id=person_id)
        if player.knkv_observed_at != observed_at:
            continue
        matches = [row for row in rows if row["PersonId"] == person_id]
        reference = next((row.get("Photo") for row in matches), None)
        if any(row.get("PrivacyLevel") not in {"OPEN", "NORMAL"} for row in matches):
            reference = None
        discover_photo(player, reference, season)


def count_private_people(rows: list[dict], hidden: set[str]) -> dict[str, int]:
    """Count anonymous rows; deduplicate only genuine IDs within a feed."""
    players: set[str] = set()
    staff: set[str] = set()
    # KNKV masks distinct people with the same literal ID; those rows are not
    # evidence of a shared identity, even if the complete rows are identical.
    anonymous = {"players": 0, "staff": 0}
    for row in rows:
        person_id = row.get("PersonId")
        if person_id not in hidden or row.get("TeamPerson") is not True:
            continue
        role = row.get("TeamPersonFunction")
        if not isinstance(role, dict):
            continue
        if role.get("RoleId") == "PLAYER_DEFAULT":
            if person_id == "PRIVATE":
                anonymous["players"] += 1
            else:
                players.add(person_id)
        elif role.get("RoleId") in {"COACHING_STAFF", "MEDICAL_STAFF", "OTHER_STAFF"}:
            if person_id == "PRIVATE":
                anonymous["staff"] += 1
            else:
                staff.add(person_id)
    return {
        "players": len(players) + anonymous["players"],
        "staff": len(staff) + anonymous["staff"],
    }
