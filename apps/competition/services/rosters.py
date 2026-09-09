"""Minimal visible roster snapshots and dated memberships from one team feed."""

from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.models import (
    MatchMembership,
    RosterMembership,
    SyncResource,
    Team,
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
VISIBLE_LEVELS = {"OPEN", "NORMAL", "LIMITED"}
ROSTER_FRESHNESS = timedelta(days=8)


@transaction.atomic
def import_roster(
    season: Season, source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Replace a complete visible roster atomically and erase withdrawn identities.

    Raises:
        ValueError: A malformed envelope cannot retire an existing roster.

    """
    team = Team.objects.select_for_update().get(season=season, external_id=source_id)
    rows = data.get("TeamPersonOverview")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid roster collection")
    if team.roster_observed_at and team.roster_observed_at > observed_at:
        return
    visible, hidden = parse_people(rows)
    withdraw_people(hidden, season, observed_at)
    current = []
    for person_id, (name, shirt, privacy, roles) in visible.items():
        if person_id in hidden:
            continue
        player, _ = Player.all_objects.get_or_create(
            knkv_person_id=person_id,
            defaults={"name": name, "knkv_observed_at": observed_at},
        )
        if player.knkv_observed_at and player.knkv_observed_at > observed_at:
            continue
        if player.archived_at is not None:
            continue
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
            last_seen_at=observed_at, shirt_number=shirt, roles=roles
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


def parse_people(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, str, str, list[str]]], set[str]]:
    """Whitelist visible players and minimal fields before applying any writes.

    Raises:
        ValueError: Required player fields are malformed.
        TypeError: The provider role is not an object.

    """
    visible: dict[str, tuple[str, str, str, list[str]]] = {}
    hidden = set()
    for row in rows:
        person_id = row.get("PersonId")
        if (
            not isinstance(person_id, str)
            or not person_id
            or len(person_id) > SOURCE_ID_LIMIT
        ):
            raise ValueError("Invalid roster identity")
        if row.get("PrivacyLevel") not in VISIBLE_LEVELS:
            hidden.add(person_id)
            continue
        role = row.get("TeamPersonFunction") or {}
        if not isinstance(role, dict):
            raise TypeError("Invalid roster role")
        role_id = role.get("RoleId")
        if row.get("TeamPerson") is not True or role_id not in {
            "PLAYER_DEFAULT",
            "COACHING_STAFF",
            "MEDICAL_STAFF",
            "OTHER_STAFF",
        }:
            continue
        parts = [row.get(key) or "" for key in ("FirstName", "Infix", "LastName")]
        if any(not isinstance(part, str) for part in parts):
            raise ValueError("Invalid roster name")
        name = " ".join(part.strip() for part in parts if part.strip())
        if not name or len(name) > NAME_LIMIT:
            raise ValueError("Invalid roster name")
        shirt = str(row.get("ShirtNumber") or "")
        if len(shirt) > SHIRT_LIMIT:
            raise ValueError("Invalid shirt number")
        previous_roles = visible[person_id][3] if person_id in visible else []
        visible[person_id] = (
            name,
            shirt,
            row["PrivacyLevel"],
            sorted({*previous_roles, role_id}),
        )
    return visible, hidden


def queue_rosters(season: Season, *, refresh_private: bool = False) -> int:
    """Discover feeds, optionally refreshing successful private-roster snapshots.

    Raises:
        ValueError: Live rosters cannot be assigned to historical seasons.

    """
    now = timezone.now()
    if not season.start_date <= timezone.localdate(now) <= season.end_date:
        raise ValueError("Roster feeds only support the current season")
    before = SyncResource.objects.filter(season=season, kind="team_roster").count()
    SyncResource.objects.bulk_create(
        [
            SyncResource(
                season=season, kind="team_roster", source_id=source_id, next_sync_at=now
            )
            for source_id in Team.objects.filter(season=season).values_list(
                "external_id", flat=True
            )
        ],
        ignore_conflicts=True,
        batch_size=1000,
    )
    refreshed = 0
    if refresh_private:
        affected = Team.objects.filter(season=season).filter(
            Q(private_roster_counts__players__gt=0)
            | Q(private_roster_counts__staff__gt=0)
        )
        # Preserve active/pending requests and retry ceilings. Only successful
        # snapshots need a fresh body to repair their anonymous counts.
        refreshed = SyncResource.objects.filter(
            season=season,
            kind="team_roster",
            source_id__in=affected.values("external_id"),
            fetched_at__isnull=False,
            failures=0,
        ).update(fetched_at=None, etag="", next_sync_at=now)
    return (
        SyncResource.objects.filter(season=season, kind="team_roster").count()
        - before
        + refreshed
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
    targets = {row.team.local_team_data_id or fallback for row in observations}
    targets.update(row.published_team_data_id for row in observations)
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
            if (row.team.local_team_data_id or fallback) == data.pk
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
        for flag, value in new_ownership[row.pk].items():
            setattr(row, flag, value)
        row.published_team_data_id = row.team.local_team_data_id or fallback
    RosterMembership.objects.bulk_update(
        observations, ["published_team_data", *ROSTER_RELATIONS.values()]
    )


def publish_pending_rosters() -> None:
    """Publish new or season-remapped observations once per global source group."""
    groups = set(
        RosterMembership.objects.filter(published_team_data=None).values_list(
            "team__group_id", flat=True
        )
    )
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
