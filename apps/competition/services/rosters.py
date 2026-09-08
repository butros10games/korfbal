"""Minimal visible roster snapshots and dated memberships from one team feed."""

from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.models import RosterMembership, SyncResource, Team
from apps.competition.services.player_photos import discover_photo
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import TeamData


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
    if RosterMembership.objects.filter(
        team=team, last_seen_at__gt=observed_at
    ).exists():
        return
    visible, hidden = _parse_roster(rows)
    # A stricter privacy observation wins even if a duplicate row says visible.
    withdrawn = Player.all_objects.filter(
        knkv_person_id__in=hidden, knkv_observed_at__lte=observed_at
    )
    for player in withdrawn:
        discover_photo(player, None, season)
    source_only = withdrawn.filter(user_id=None)
    TeamData.players.through.objects.filter(
        player_id__in=source_only.values("pk")
    ).delete()
    owned_links = (
        RosterMembership.objects
        .filter(player_id__in=withdrawn.values("pk"), local_link_created=True)
        .exclude(published_team_data=None)
        .values_list("player_id", "published_team_data_id")
    )
    remove_links = Q(pk__in=[])
    for player_id, team_data_id in owned_links:
        remove_links |= Q(player_id=player_id, teamdata_id=team_data_id)
    TeamData.players.through.objects.filter(remove_links).delete()
    RosterMembership.objects.filter(player_id__in=withdrawn.values("pk")).delete()
    source_only.update(name="", knkv_privacy="PRIVATE", knkv_observed_at=observed_at)
    withdrawn.exclude(user_id=None).update(
        knkv_privacy="PRIVATE", knkv_observed_at=observed_at
    )
    current = []
    for person_id, (name, shirt, privacy) in visible.items():
        if person_id in hidden:
            continue
        player, _ = Player.all_objects.get_or_create(
            knkv_person_id=person_id,
            defaults={"name": name, "knkv_observed_at": observed_at},
        )
        if player.knkv_observed_at and player.knkv_observed_at > observed_at:
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
            last_seen_at=observed_at, shirt_number=shirt
        )
        current.append(membership.pk)
    RosterMembership.objects.filter(team=team, ended_at=None).exclude(
        pk__in=current
    ).update(ended_at=observed_at)
    _discover_photos(rows, set(visible) - hidden, season, observed_at)
    publish_roster(team)


def _parse_roster(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, str, str]], set[str]]:
    """Whitelist visible players and minimal fields before applying any writes.

    Raises:
        ValueError: Required player fields are malformed.
        TypeError: The provider role is not an object.

    """
    visible: dict[str, tuple[str, str, str]] = {}
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
        if row.get("TeamPerson") is not True or role.get("RoleId") != "PLAYER_DEFAULT":
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
        visible[person_id] = (name, shirt, row["PrivacyLevel"])
    return visible, hidden


def queue_rosters(season: Season) -> int:
    """Discover one weekly feed per source variant without resetting checkpoints.

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
    return (
        SyncResource.objects.filter(season=season, kind="team_roster").count() - before
    )


@transaction.atomic
def publish_roster(team: Team) -> None:
    """Publish source observations into the ordinary season-specific team roster."""
    group = team.group
    if group is None or group.local_team_data_id is None:
        return
    team_data = TeamData.objects.select_for_update().get(pk=group.local_team_data_id)
    observations = RosterMembership.objects.filter(team__group=group)
    observations.filter(published_team_data=None).update(published_team_data=team_data)
    desired = set(
        observations.filter(
            ended_at=None,
            last_seen_at__gte=timezone.now() - ROSTER_FRESHNESS,
            player_id__in=Player.objects.values("pk"),
        ).values_list("player_id", flat=True)
    )
    through = TeamData.players.through
    present = set(
        through.objects.filter(teamdata_id=team_data.pk).values_list(
            "player_id", flat=True
        )
    )
    for player_id in desired - present:
        through.objects.create(teamdata_id=team_data.pk, player_id=player_id)
        observation = (
            observations
            .filter(player_id=player_id, ended_at=None)
            .order_by("pk")
            .first()
        )
        if observation:
            observation.local_link_created = True
            observation.save(update_fields=("local_link_created",))
    owned = set(
        observations.filter(local_link_created=True).values_list("player_id", flat=True)
    )
    through.objects.filter(
        teamdata_id=team_data.pk, player_id__in=owned - desired
    ).delete()


def publish_pending_rosters() -> None:
    """Finish roster links once native team publication has resolved the season."""
    teams = Team.objects.filter(
        roster_memberships__published_team_data=None,
        roster_memberships__isnull=False,
        group__local_team_data__isnull=False,
    ).distinct()
    for team in teams:
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
