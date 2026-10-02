"""Helpers for keeping match player groups consistent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.db.models.expressions import Combinable
from django.utils import timezone

from apps.game_tracker.models import (
    GroupType,
    MatchData,
    MatchGuestPlayer,
    PlayerChange,
    PlayerGroup,
    StartingPlayerAssignment,
    SubstitutionEventDetail,
)
from apps.game_tracker.services.live_update_signal_control import (
    suppress_tracker_delete_side_effects,
)
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


RESERVE_GROUP_NAME = "Reserve"


@dataclass(slots=True)
class PlayerGroupAssignmentError(ValueError):
    """Raised when a player-group mutation would break tracker rules."""

    message: str

    def __str__(self) -> str:
        """Return the user-facing error string."""
        return self.message


def club_lineup_players(*, match: Match, team: Team) -> QuerySet[Player]:
    """Return the club's eligible picker candidates for this match's date/season.

    Guests added for this match and team stay eligible so a removed guest can
    be selected again; they never become candidates for any other match.
    """
    match_date = timezone.localdate(match.start_time)
    season_rosters = TeamData.objects.filter(
        team__club_id=team.club_id,
        season_id=match.season_id,
    )
    memberships = PlayerClubMembership.objects.filter(
        player_id=OuterRef("pk"),
        club_id=team.club_id,
        start_date__lte=match_date,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=match_date))
    return Player.objects.filter(
        Exists(season_rosters.filter(players=OuterRef("pk")))
        | Exists(season_rosters.filter(coach=OuterRef("pk")))
        | Exists(memberships)
        | Exists(match_guests_for(match=match, team=team).filter(player=OuterRef("pk")))
    )


def match_guests_for(*, match: Match, team: Team) -> QuerySet[MatchGuestPlayer]:
    """Return the guest links for one match-team selection."""
    return MatchGuestPlayer.objects.filter(match_data__match_link=match, team=team)


def _missing_player_groups(
    match_data_id: object, team_ids: tuple[str, str], group_types: list[GroupType]
) -> list[PlayerGroup]:
    existing_group_keys = set(
        PlayerGroup.objects.filter(
            match_data_id=match_data_id, team_id__in=team_ids
        ).values_list("team_id", "starting_type_id")
    )
    return [
        PlayerGroup(
            match_data_id=match_data_id,
            team_id=team_id,
            starting_type=group_type,
            current_type=group_type,
        )
        for team_id in team_ids
        for group_type in group_types
        if (team_id, group_type.id_uuid) not in existing_group_keys
    ]


def ensure_player_groups_for_match_data(match_data: MatchData) -> None:
    """Create any missing PlayerGroup rows for both teams in a match.

    Groups are created when a lineup or tracker first uses a match rather than for
    every imported fixture: almost all imported matches are never tracked. Readers
    that only aggregate groups treat a missing lineup as empty.
    """
    group_types = list(GroupType.objects.order_by("order", "name"))
    if not group_types:
        return
    team_ids = (
        Match.objects
        .filter(pk=match_data.match_link_id)
        .values_list("home_team_id", "away_team_id")
        .get()
    )
    if not _missing_player_groups(match_data.pk, team_ids, group_types):
        return

    # Concurrent first reads of the same lineup must not create duplicate groups.
    with transaction.atomic():
        list(
            MatchData.objects
            .select_for_update()
            .filter(pk=match_data.pk)
            .values_list("pk", flat=True)
        )
        missing_groups = _missing_player_groups(match_data.pk, team_ids, group_types)
        if missing_groups:
            PlayerGroup.objects.bulk_create(missing_groups)


def ensure_player_groups_for_group_type(group_type: GroupType) -> None:
    """Add a new group type to the lineups that already exist.

    Matches without groups receive every type when their lineup is first used.
    """
    with_type = set(
        PlayerGroup.objects.filter(starting_type=group_type).values_list(
            "match_data_id", "team_id"
        )
    )
    lineups = (
        PlayerGroup.objects
        .order_by()
        .values_list("match_data_id", "team_id")
        .distinct()
    )
    PlayerGroup.objects.bulk_create(
        [
            PlayerGroup(
                match_data_id=match_data_id,
                team_id=team_id,
                starting_type=group_type,
                current_type=group_type,
            )
            for match_data_id, team_id in lineups
            if (match_data_id, team_id) not in with_type
        ],
        batch_size=1000,
    )


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Lineups (matches) and groups removed, or found in a dry run."""

    matches: int
    groups: int


def prune_unused_player_groups(
    *, started_before: datetime, batch_size: int = 2000, dry_run: bool = False
) -> PruneResult:
    """Delete never-used lineups that every imported match used to receive.

    A match's groups are removed together, and only when none has players, a
    captured starting lineup or a substitution; such a lineup is recreated with
    new IDs on first use. Matches that have not started are left alone so an
    editor holding their group IDs keeps working.
    """
    used = (
        Q(players__isnull=False)
        | Exists(StartingPlayerAssignment.objects.filter(player_group=OuterRef("pk")))
        | Exists(PlayerChange.objects.filter(player_group=OuterRef("pk")))
        | Exists(SubstitutionEventDetail.objects.filter(player_group=OuterRef("pk")))
    )
    match_data_ids = (
        MatchData.objects
        .filter(
            match_link__start_time__lt=started_before,
            pk__in=PlayerGroup.objects.values("match_data_id"),
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    matches = groups = 0
    batch: list[object] = []
    for match_data_id in match_data_ids.iterator(chunk_size=batch_size):
        batch.append(match_data_id)
        if len(batch) == batch_size:
            pruned = _prune_batch(batch, used, dry_run=dry_run)
            matches, groups = matches + pruned.matches, groups + pruned.groups
            batch = []
    if batch:
        pruned = _prune_batch(batch, used, dry_run=dry_run)
        matches, groups = matches + pruned.matches, groups + pruned.groups
    return PruneResult(matches=matches, groups=groups)


def _unused_lineups(batch: list[object], used: Combinable) -> list[object]:
    in_use = set(
        PlayerGroup.objects.filter(used, match_data_id__in=batch).values_list(
            "match_data_id", flat=True
        )
    )
    return [match_data_id for match_data_id in batch if match_data_id not in in_use]


def _prune_batch(
    batch: list[object], used: Combinable, *, dry_run: bool
) -> PruneResult:
    candidates = _unused_lineups(batch, used)
    if dry_run:
        groups = PlayerGroup.objects.filter(match_data_id__in=candidates).count()
        return PruneResult(matches=len(candidates), groups=groups)
    if not candidates:
        return PruneResult(matches=0, groups=0)
    # Lineup writers lock MatchData; take the same locks (in a stable order) and
    # decide again under them, so a lineup saved after the first check survives.
    # Removing empty groups changes no statistics; skip per-row recompute jobs.
    with transaction.atomic(), suppress_tracker_delete_side_effects():
        locked = list(
            MatchData.objects
            .select_for_update()
            .filter(pk__in=candidates)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        unused = _unused_lineups(locked, used)
        groups = PlayerGroup.objects.filter(match_data_id__in=unused)
        deleted = groups.delete()[1].get(PlayerGroup._meta.label, 0)
    return PruneResult(matches=len(unused), groups=deleted)


def get_reserve_group(*, match_data: MatchData, team: Team) -> PlayerGroup:
    """Return the team's reserve group for a match, creating the lineup if needed."""
    reserve = PlayerGroup.objects.filter(
        team=team,
        match_data=match_data,
        starting_type__name=RESERVE_GROUP_NAME,
    )
    group = reserve.first()
    if group is None:
        ensure_player_groups_for_match_data(match_data)
        group = reserve.get()
    return group


def add_player_to_group(
    *,
    player: Player,
    target_group: PlayerGroup,
    source_group: PlayerGroup | None = None,
) -> None:
    """Move one actual member, keeping each player in one group per match.

    Raises:
        PlayerGroupAssignmentError: The source is false, another assignment would
            remain, or a court move does not come from the reserve group.

    """
    current_group_ids = set(
        PlayerGroup.objects.filter(
            match_data=target_group.match_data, players=player
        ).values_list("pk", flat=True)
    )
    if source_group is not None and source_group.pk not in current_group_ids:
        raise PlayerGroupAssignmentError("Player is not in the selected source group")

    effective_source_group = source_group
    if target_group.starting_type.name != RESERVE_GROUP_NAME:
        reserve_group = get_reserve_group(
            match_data=target_group.match_data,
            team=target_group.team,
        )
        if effective_source_group is None and reserve_group.pk in current_group_ids:
            effective_source_group = reserve_group
        if (
            effective_source_group is None
            or effective_source_group.pk != reserve_group.pk
        ):
            raise PlayerGroupAssignmentError(
                f"{player} is not in the reserve player group.",
            )

    allowed_group_ids = {target_group.pk}
    if effective_source_group is not None:
        allowed_group_ids.add(effective_source_group.pk)
    if current_group_ids - allowed_group_ids:
        raise PlayerGroupAssignmentError("Player is already in another player group")
    if (
        effective_source_group is not None
        and effective_source_group.pk != target_group.pk
    ):
        effective_source_group.players.remove(player)
    target_group.players.add(player)
