"""Publish provider snapshots into the same identities used by native app records."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import time
from typing import Any
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, QuerySet, Value
from django.db.models.functions import Concat
from django.utils import timezone

from apps.club.models import Club as AppClub
from apps.competition.models import (
    Club,
    Match,
    Pool,
    PoolEntry,
    SyncLease,
    Team,
    TeamGroup,
    TeamParticipation,
)
from apps.competition.services.competition_periods import (
    period_backlog,
    resolve_pool_periods,
)
from apps.competition.services.fixture_linkage import (
    desired_fixture,
    fixture_decision,
    native_fixture,
)
from apps.competition.services.identities import (
    merge_unlinked_joint_groups,
    unnamed_pool_label,
)
from apps.competition.services.logos import publish_logo
from apps.competition.services.match_rules import RULE_RELATIONS, source_rules
from apps.competition.services.reconciliation import (
    LOCAL_FIELDS,
    SOURCE_MODELS,
    JointTeamIndex,
    Reconciler,
    normalized,
    team_label,
)
from apps.competition.services.rosters import publish_pending_rosters
from apps.competition.services.schedule_notifications import (
    ScheduleChangeDispatcher,
    schedule_changed,
)
from apps.competition.services.seasons import SeasonResolver
from apps.game_tracker.models import MatchData, MatchPart, Shot
from apps.game_tracker.services.match_rule_profiles import (
    UNCHANGED,
    apply_rule_profile,
)
from apps.schedule.models import (
    Match as AppMatch,
    Season,
    SeasonPool,
)
from apps.team.models import (
    Team as AppTeam,
    TeamData,
)


def display_team_name(name: str, club: str) -> str:
    """Strip only an exact club prefix, retaining the team's display capitalization."""
    parts = name.split()
    prefix = len(club.split())
    return (
        " ".join(parts[prefix:])
        if normalized(" ".join(parts[:prefix])) == normalized(club)
        else name
    )


def pool_memberships(pool_ids: list[int]) -> dict[int, set[Any]]:
    """Collect membership from both published poules and observed fixtures."""
    members: dict[int, set[Any]] = defaultdict(set)
    for pool, team in PoolEntry.objects.filter(
        pool_id__in=pool_ids, team__group__local_team__isnull=False
    ).values_list("pool_id", "team__group__local_team_id"):
        members[pool].add(team)
    for side in ("home_team", "away_team"):
        for pool, team in Match.objects.filter(
            pool_id__in=pool_ids, **{side + "__group__local_team__isnull": False}
        ).values_list("pool_id", side + "__group__local_team_id"):
            members[pool].add(team)
    return members


def native_membership(team_path: str) -> Exists:
    """Match rows whose linked team is already listed by the native poule."""
    return Exists(
        SeasonPool.teams.through.objects.filter(
            seasonpool_id=OuterRef("pool__local_pool_id"),
            team_id=OuterRef(team_path + "__group__local_team_id"),
        )
    )


def pools_to_publish() -> QuerySet[Pool]:
    """Poules that are unpublished, still unnamed, or miss a native membership.

    Missing memberships are collected once per source table rather than per
    poule: a correlated check walks every historical fixture again for each of
    the ~15,000 poules (seconds per pass), while an uncorrelated anti-join reads
    each table once.
    """
    unlisted = [
        model.objects
        .filter(**{team_path + "__group__local_team__isnull": False})
        .exclude(native_membership(team_path))
        .values("pool_id")
        for model, team_path in (
            (PoolEntry, "team"),
            (Match, "home_team"),
            (Match, "away_team"),
        )
    ]
    placeholder = Q(local_pool__name=Concat(Value("KNKV-poule "), "external_id"))
    named = ~Q(name="") | ~Q(class_name="")
    return Pool.objects.filter(
        Q(local_pool=None)
        | Q(local_pool__name="")
        # A placeholder can only be renamed once the source knows a name.
        | (placeholder & named)
        | Q(pk__in=unlisted[0])
        | Q(pk__in=unlisted[1])
        | Q(pk__in=unlisted[2])
    )


# Manually entered fixtures sometimes store Dutch local time as UTC: one or two
# hours away from the official kickoff, depending on daylight saving time.
CLOCK_SHIFTS = tuple(timedelta(hours=hours) for hours in (1, -1, 2, -2))


def clock_twin(
    row: Match,
    teams: tuple[UUID, UUID],
    candidates: dict[tuple[Any, ...], list[AppMatch]],
    claimed: set[UUID],
) -> list[AppMatch]:
    """Find the one unlinked native fixture a whole clock offset away, if any.

    Returns:
        The single matching fixture, or an empty list when none or several match.

    """
    twins = [
        match
        for shift in CLOCK_SHIFTS
        for match in candidates[row.season_id, *teams, row.starts_at + shift]
        if match.pk not in claimed
    ]
    return twins if len(twins) == 1 else []


class Publisher:
    """Resolve and create identities in dependency order using shared lookup indexes."""

    def __init__(self, schedule_changes: ScheduleChangeDispatcher | None) -> None:
        """Collect publication counts and reviewable conflicts.

        ``schedule_changes`` is None only for repairs that never publish fixtures.
        """
        self.schedule_changes = schedule_changes
        self.fresh: set[UUID] = set()
        # Native fixture -> (source record, status) already linked to it.
        self.claimants: dict[UUID, tuple[int, str]] = {}
        # Highest source fixture ID this pass selected (the sweep cursor).
        self.last_match: int | None = None
        self.counts: Counter[str] = Counter()
        self.blocked: list[dict[str, Any]] = []
        # (source team, period) -> native season of fixtures routed by period.
        self.participating: dict[tuple[int, str], UUID] = {}
        self.participation_groups: set[int] = set()

    def conflict(self, kind: str, source_id: int, reason: str) -> None:
        """Retain unresolved identities for explicit review instead of guessing."""
        self.blocked.append({"kind": kind, "source_id": source_id, "reason": reason})

    def clubs(self) -> None:
        """Reuse reviewed club links and create only unclaimed unique names."""
        # Linked identities need no name reconciliation. Only the monotonic
        # status mismatch is publication work; manual names and images survive.
        dissolved_ids = list(
            Club.objects.filter(
                dissolved=True, local_club__dissolved=False
            ).values_list("local_club_id", flat=True)
        )
        if dissolved_ids:
            self.counts["clubs_dissolved"] += AppClub.objects.filter(
                pk__in=dissolved_ids, dissolved=False
            ).update(dissolved=True)
        local_names: dict[str, list[AppClub]] = defaultdict(list)
        for club in AppClub.objects.all():
            local_names[normalized(club.name)].append(club)
        sources = list(Club.objects.all())
        source_names = Counter(normalized(row.name) for row in sources)
        claimed = {row.local_club_id for row in sources if row.local_club_id}
        for row in sources:
            if row.local_club_id:
                continue
            if row.dissolved:
                self.dissolved_club(row, local_names)
                continue
            key = normalized(row.name)
            candidates = local_names[key]
            if (
                source_names[key] != 1
                or len(candidates) > 1
                or (candidates and candidates[0].pk in claimed)
            ):
                self.conflict("club", row.pk, "ambiguous_name")
                continue
            if candidates:
                local = candidates[0]
            else:
                local = AppClub.objects.create(name=row.name)
                local_names[key].append(local)
                self.counts["clubs_created"] += 1
            row.local_club = local
            row.save(update_fields=("local_club",))
            claimed.add(local.pk)

    def dissolved_club(self, row: Club, local_names: dict[str, list[AppClub]]) -> None:
        """Create a separate native club for a club that no longer exists."""
        if not row.name:
            self.conflict("club", row.pk, "club_name_missing")
            return
        for name in (row.name, f"{row.name} (opgeheven)"):
            if local_names[normalized(name)]:
                continue
            local = AppClub.objects.create(name=name, dissolved=True)
            local_names[normalized(name)].append(local)
            row.local_club = local
            row.save(update_fields=("local_club",))
            self.counts["clubs_created"] += 1
            return
        self.conflict("club", row.pk, "dissolved_name_taken")

    def teams(self) -> None:
        """Use a global Team plus exactly one TeamData per team and season."""
        sources = list(
            TeamGroup.objects
            .filter(
                Q(local_team_data__isnull=True)
                | Q(
                    pk__in=Team.objects.filter(local_team_data__isnull=True).values(
                        "group_id"
                    )
                )
            )
            .select_related("club")
            .order_by("pk")
        )
        if not sources:
            return
        index: dict[tuple[str, str], list[AppTeam]] = defaultdict(list)
        joint = JointTeamIndex()
        local_teams: dict[str, AppTeam] = {}
        for team in AppTeam.objects.select_related("club"):
            index[str(team.club_id), team_label(team.name, team.club.name)].append(team)
            joint.add(str(team.pk), team.club.name, team.name)
            local_teams[str(team.pk)] = team
        claimed = {
            (str(season_id), str(local_team_id)): pk
            for pk, season_id, local_team_id in TeamGroup.objects.exclude(
                local_team=None
            ).values_list("pk", "season_id", "local_team_id")
        }
        for row in sources:
            if row.local_team_id is None:
                if row.club.local_club_id is None:
                    self.conflict("team", row.pk, "club_unresolved")
                    continue
                key = (str(row.club.local_club_id), team_label(row.name, row.club.name))
                candidates = {str(team.pk): team for team in index[key]}
                candidates.update({
                    pk: local_teams[pk] for pk in joint.matches(row.name, row.club.name)
                })
                if len(candidates) > 1:
                    self.conflict("team", row.pk, "ambiguous_team")
                    continue
                local = next(iter(candidates.values()), None)
                if local and (str(row.season_id), str(local.pk)) in claimed:
                    self.conflict("team", row.pk, "season_team_already_claimed")
                    continue
                if local is None:
                    local = AppTeam.objects.create(
                        club_id=row.club.local_club_id,
                        name=display_team_name(row.name, row.club.name),
                    )
                    index[key].append(local)
                    self.counts["teams_created"] += 1
                row.local_team = local
                claimed[str(row.season_id), str(local.pk)] = row.pk
            team_data, created = TeamData.objects.get_or_create(
                team_id=row.local_team_id, season_id=row.season_id
            )
            self.counts["team_seasons_created"] += int(created)
            if row.local_team_data_id != team_data.pk:
                row.local_team_data = team_data
                row.save(update_fields=("local_team", "local_team_data"))

        self.team_variants()

    def team_variants(
        self, *, force: bool = False, scope: Season | None = None
    ) -> None:
        """Bind each source variant to its native team and playing season."""
        variants = Team.objects.select_related("group", "local_team_data")
        if scope is not None:
            variants = variants.filter(season=scope)
        if not force:
            variants = variants.filter(local_team_data=None)
        variants = list(variants)
        resolver = SeasonResolver()
        # Only the bound native teams' seasons: the full table has ~80,000 rows.
        existing = {
            (row.team_id, row.season_id): row
            for row in TeamData.objects.filter(
                team_id__in={
                    variant.group.local_team_id
                    for variant in variants
                    if variant.group and variant.group.local_team_id
                }
            )
        }
        changed = []
        for variant in variants:
            if not variant.group or not variant.group.local_team_id:
                continue
            season_id = resolver.resolve(variant.season_id, variant.sport)
            if season_id is None:
                self.conflict("team", variant.pk, "season_discipline_unresolved")
                continue
            key = (variant.group.local_team_id, season_id)
            data = existing.get(key)
            if data is None:
                data, created = TeamData.objects.get_or_create(
                    team_id=key[0], season_id=key[1]
                )
                existing[key] = data
                self.counts["team_seasons_created"] += int(created)
            if variant.local_team_data_id != data.pk:
                variant.local_team_data = data
                changed.append(variant)
        Team.objects.bulk_update(changed, ["local_team_data"], batch_size=1000)

    def context_backlog(self) -> None:
        """Resolve one configured page of previously unattempted pool contexts."""
        backlog_limit = max(
            0, min(5000, getattr(settings, "SPORTLINK_CONTEXT_BACKLOG_LIMIT", 0))
        )
        if backlog_limit:
            self.periods(period_backlog(limit=backlog_limit))

    def pools(self) -> None:
        """Publish poules once and share membership through global teams."""
        self.context_backlog()
        rows = list(pools_to_publish().select_related("local_pool").order_by("pk"))
        if not rows:
            return
        resolver = SeasonResolver()
        self.periods(
            row.pk
            for row in rows
            if not row.phase or resolver.splits(row.season_id, row.sport)
        )
        rows = list(
            Pool.objects
            .filter(pk__in=[row.pk for row in rows])
            .select_related("local_pool")
            .order_by("pk")
        )
        members = pool_memberships([row.pk for row in rows])
        claimed = set(
            Pool.objects.exclude(local_pool=None).values_list(
                "local_pool_id", flat=True
            )
        )
        through = SeasonPool.teams.through
        existing_members = set(
            through.objects.filter(
                seasonpool_id__in={row.local_pool.pk for row in rows if row.local_pool}
            ).values_list("seasonpool_id", "team_id")
        )
        additions = []
        for row in rows:
            season_id = self.pool_season(row, resolver)
            if season_id is None:
                continue
            local = row.local_pool
            if local and local.season_id != season_id:
                self.conflict("pool", row.pk, "season_repair_required")
                continue
            fallback = unnamed_pool_label(row.external_id)
            name = f"{row.class_name} {row.name}".strip() or fallback
            if (
                local is not None
                and local.name in {"", fallback}
                and local.name != name
                and not SeasonPool.objects
                .filter(season_id=season_id, name=name, sport=local.sport)
                .exclude(pk=local.pk)
                .exists()
            ):
                local.name = name
                local.save(update_fields=("name",))
            if local is None:
                local = self.claim_pool(row, season_id, name, claimed)
                if local is None:
                    continue
            for team_id in members[row.pk]:
                pair = (local.pk, team_id)
                if pair not in existing_members:
                    additions.append(through(seasonpool_id=local.pk, team_id=team_id))
                    existing_members.add(pair)
        through.objects.bulk_create(additions, ignore_conflicts=True, batch_size=1000)

    def pool_season(self, row: Pool, resolver: SeasonResolver) -> UUID | None:
        """Return a poule's native season, or None after recording why not."""
        if not row.phase and resolver.splits(row.season_id, row.sport):
            # Its competition period decides the native season; wait for it.
            self.counts["pools_period_pending"] += 1
            return None
        season_id = resolver.resolve(row.season_id, row.sport, row.phase)
        if season_id is None:
            self.conflict("pool", row.pk, "season_discipline_unresolved")
        return season_id

    def claim_pool(
        self, row: Pool, season_id: UUID, name: str, claimed: set[UUID]
    ) -> SeasonPool | None:
        """Create or reuse an unclaimed native poule and link the source to it."""
        local, created = SeasonPool.objects.get_or_create(
            season_id=season_id, name=name, sport=row.sport
        )
        if not created and local.pk in claimed:
            suffix = f" [KNKV {row.external_id}]"
            name = name[: 512 - len(suffix)] + suffix
            local, created = SeasonPool.objects.get_or_create(
                season_id=season_id, name=name, sport=row.sport
            )
            if not created and local.pk in claimed:
                self.conflict("pool", row.pk, "pool_already_claimed")
                return None
        self.counts["pools_created"] += int(created)
        row.local_pool = local
        row.save(update_fields=("local_pool",))
        claimed.add(local.pk)
        return local

    def link_fixture(
        self,
        row: Match,
        teams: tuple[UUID, UUID],
        candidates: dict[tuple[Any, ...], list[AppMatch]],
        claimed: set[UUID],
        pool_id: UUID | None,
    ) -> bool:
        """Link an unlinked record to its native fixture, creating it if new.

        Returns:
            Whether the record is linked; otherwise the conflict is recorded.

        """
        key = (row.season_id, *teams, row.starts_at)
        existing = candidates[key]
        taken = len(existing) == 1 and existing[0].pk in claimed
        if taken and self.take_over(row, existing[0].pk):
            claimed.discard(existing[0].pk)
            taken = False
        if len(existing) > 1 or taken:
            self.conflict("match", row.pk, "ambiguous_fixture")
            return False
        if not existing:
            existing = clock_twin(row, teams, candidates, claimed)
            if existing:
                self.counts["matches_clock_twin"] += 1
        if existing:
            local = existing[0]
        else:
            local = AppMatch.objects.create(
                season_id=row.season_id,
                home_team_id=teams[0],
                away_team_id=teams[1],
                pool_id=pool_id,
                start_time=row.starts_at,
            )
            row.local_created = True
            self.fresh.add(local.pk)
            candidates[key].append(local)
            self.counts["matches_created"] += 1
        row.local_match = local
        claimed.add(local.pk)
        return True

    def take_over(self, row: Match, local_id: UUID) -> bool:
        """Move a fixture from its superseded twin to this final record.

        Returns:
            Whether the fixture is free for this record now.

        """
        claimant = self.claimants.get(local_id)
        final = (
            row.status == "FINAL"
            and row.home_score is not None
            and row.away_score is not None
        )
        if claimant is None or not final or claimant[1] not in SUPERSEDED_STATUSES:
            return False
        twin = Match.objects.select_for_update(no_key=True).get(pk=claimant[0])
        # The final record inherits the twin's ownership and published result, so
        # manual edits made since that publication still block the update.
        row.local_created = twin.local_created
        row.published_state = twin.published_state
        row.published_schedule = twin.published_schedule
        row.schedule_notification_id = twin.schedule_notification_id
        Match.objects.filter(pk=twin.pk).update(
            local_match=None,
            local_created=False,
            published_state={},
            published_schedule={},
            schedule_notification_id=None,
        )
        self.counts["matches_superseded"] += 1
        return True

    def periods(self, pool_ids: Iterable[int]) -> None:
        """Record competition periods of the poules this pass publishes."""
        for key, value in resolve_pool_periods(pool_ids).items():
            if value:
                self.counts[f"pool_periods_{key}"] += value

    def match_seasons(self, rows: list[Match]) -> list[Match]:
        """Resolve discipline and competition period before matching identities.

        A fixture follows its poule's period, so a rescheduled fixture stays in
        its competition instead of moving with its calendar date.
        """
        resolver = SeasonResolver()
        source_sports = dict(
            Team.objects.filter(
                pk__in={
                    team
                    for row in rows
                    for team in (row.home_team_id, row.away_team_id)
                }
            ).values_list("pk", "sport")
        )
        # Published poules are rechecked too: changed evidence is recorded for
        # review, never applied (see resolve_pool_periods).
        self.periods(row.pool_id for row in rows if row.pool_id is not None)
        pool_rows = Pool.objects.filter(
            pk__in={row.pool_id for row in rows if row.pool_id is not None}
        ).values_list("pk", "phase", "phase_evidence", "local_pool__season_id")
        phases = {pk: phase for pk, phase, _, _ in pool_rows}
        # A published poule stays in one native season: a new fixture never
        # publishes elsewhere while the poule waits for a reviewed repair.
        pool_seasons = {pk: season for pk, _, _, season in pool_rows if season}
        # New evidence contradicts a published period: new fixtures wait for the
        # reviewed correction instead of joining a period they may not belong to.
        under_review = {pk for pk, _, evidence, _ in pool_rows if "review" in evidence}
        routing_review = {
            pk
            for pk, _, evidence, _ in pool_rows
            if evidence.get("review", {}).get("reason") == "season_repair_required"
        }
        eligible = []
        for row in rows:
            home_sport, away_sport = (
                source_sports[row.home_team_id],
                source_sports[row.away_team_id],
            )
            phase = phases.get(row.pool_id, "") if row.pool_id is not None else ""
            if row.local_match_id is None and row.pool_id in under_review:
                if row.pool_id in routing_review:
                    self.conflict("match", row.pk, "season_repair_required")
                self.counts["matches_period_review"] += 1
                continue
            if (
                row.pool_id is not None
                and not phase
                and resolver.splits(row.season_id, home_sport)
            ):
                self.counts["matches_period_pending"] += 1
                continue
            season_id = resolver.resolve(row.season_id, home_sport, phase)
            if season_id is None or home_sport != away_sport:
                self.conflict("match", row.pk, "season_discipline_unresolved")
                continue
            if row.local_match_id and row.local_match.season_id != season_id:
                self.conflict("match", row.pk, "season_repair_required")
                continue
            if pool_seasons.get(row.pool_id, season_id) != season_id:
                self.conflict("match", row.pk, "season_repair_required")
                continue
            if phase and season_id != resolver.resolve(row.season_id, home_sport):
                for team_id in (row.home_team_id, row.away_team_id):
                    self.participating[team_id, phase] = season_id
            row.season_id = season_id
            eligible.append(row)
        return eligible

    def participations(self) -> None:
        """Give teams of period-routed fixtures a roster in that period's season.

        Existing participations are kept: a team's roster in an earlier period
        is history, and its native TeamData identity never changes.
        """
        if not self.participating:
            return
        groups = dict(
            Team.objects.filter(
                pk__in={team for team, _ in self.participating}
            ).values_list("pk", "group__local_team_id")
        )
        existing = set(
            TeamParticipation.objects.filter(
                team_id__in={team for team, _ in self.participating}
            ).values_list("team_id", "phase")
        )
        created = []
        for (team_id, phase), season_id in sorted(self.participating.items()):
            local_team = groups.get(team_id)
            if local_team is None or (team_id, phase) in existing:
                continue
            data, made = TeamData.objects.get_or_create(
                team_id=local_team, season_id=season_id
            )
            self.counts["team_seasons_created"] += int(made)
            created.append(
                TeamParticipation(team_id=team_id, phase=phase, team_data=data)
            )
        TeamParticipation.objects.bulk_create(created, ignore_conflicts=True)
        self.counts["participations_created"] += len(created)
        self.participation_groups.update(
            Team.objects.filter(
                pk__in={row.team_id for row in created}, group__isnull=False
            ).values_list("group_id", flat=True)
        )

    def matches(self, bounds: MatchBounds | None = None) -> None:
        """Publish fixtures and results while keeping tracked history authoritative.

        ``limit``, ``seasons`` and ``deadline`` bound one pass; unpublished rows
        stay pending for the next pass, oldest first.
        """
        bounds = bounds or MatchBounds()
        rows = pending_rows(bounds)
        if not rows:
            return
        self.last_match = rows[-1].pk
        deadline = bounds.deadline
        rows = self.match_seasons(rows)
        self.participations()
        team_ids = {
            team_id for row in rows for team_id in (row.home_team_id, row.away_team_id)
        }
        teams = dict(
            Team.objects.filter(pk__in=team_ids).values_list(
                "pk", "group__local_team_id"
            )
        )
        pools = dict(
            Pool.objects.filter(
                pk__in={row.pool_id for row in rows if row.pool_id is not None}
            ).values_list("pk", "local_pool_id")
        )
        unlinked = [row for row in rows if row.local_match_id is None]
        candidates: dict[tuple[Any, ...], list[AppMatch]] = defaultdict(list)
        claimed: set[UUID] = set()
        if unlinked:
            # Linked result refreshes already have their fixture identity. Only
            # unresolved fixtures need the native catalogue's matching candidates.
            native = list(
                AppMatch.objects.filter(
                    season_id__in={row.season_id for row in unlinked},
                    home_team_id__in={teams.get(row.home_team_id) for row in unlinked},
                    away_team_id__in={teams.get(row.away_team_id) for row in unlinked},
                    start_time__in={
                        row.starts_at + shift
                        for row in unlinked
                        for shift in (timedelta(0), *CLOCK_SHIFTS)
                    },
                )
            )
            for match in native:
                candidates[
                    match.season_id,
                    match.home_team_id,
                    match.away_team_id,
                    match.start_time,
                ].append(match)
            self.claimants = {
                local: (source, status)
                for source, local, status in Match.objects.filter(
                    local_match_id__in=[match.pk for match in native]
                ).values_list("pk", "local_match_id", "status")
            }
            claimed = set(self.claimants)
        for row in rows:
            if deadline is not None and time.monotonic() >= deadline:
                self.counts["matches_deferred"] += 1
                continue
            home, away = teams.get(row.home_team_id), teams.get(row.away_team_id)
            if home is None or away is None:
                self.conflict("match", row.pk, "team_unresolved")
                continue
            if row.local_match_id is None and not self.link_fixture(
                row, (home, away), candidates, claimed, pools.get(row.pool_id)
            ):
                continue
            tracker = MatchData.objects.select_for_update(no_key=True).get(
                match_link_id=row.local_match_id
            )
            accepted = self.matched_result(
                row, tracker, pools.get(row.pool_id), home, away
            )
            schedule_fields = self.schedule(row, accepted=accepted)
            # Published as of the change read above: a concurrent import raises
            # updated_at past it, so the fixture stays pending for the next pass.
            row.published_at = row.updated_at
            row.save(
                update_fields=(
                    "local_match",
                    "local_created",
                    "published_at",
                    "published_state",
                    *schedule_fields,
                )
            )

    def matched_result(
        self,
        row: Match,
        tracker: MatchData,
        pool_id: UUID | None,
        home: UUID,
        away: UUID,
    ) -> bool:
        """Validate native participants before adopting source scores or rules."""
        # Validate sporting identity before attributing source scores or rules.
        fixture_accepted = self.fixture(row, tracker, pool_id, home, away)
        accepted = False
        if fixture_accepted:
            # An imported final result is not tracking history.
            outcome = apply_rule_profile(tracker, source_rules(row))
            if outcome != UNCHANGED:
                self.counts[f"rules_{outcome}"] += 1
        native = row.local_match
        compatible_sides = (
            str(native.home_team_id) == str(home)
            and str(native.away_team_id) == str(away)
            and native.season_id == row.season_id
        )
        if compatible_sides:
            accepted = self.result(row, tracker, pool_id)
        return accepted

    def fixture(
        self,
        row: Match,
        tracker: MatchData,
        pool_id: UUID | None,
        home: UUID,
        away: UUID,
    ) -> bool:
        """Update only native values still equal to the retained importer baseline."""
        local = row.local_match
        current = native_fixture(local)
        desired = desired_fixture(row, home, away, pool_id)
        decision = fixture_decision(row, tracker, current, desired)
        if decision not in {"unchanged", "safe_update"}:
            self.conflict("match", row.pk, decision)
            return False
        if decision == "safe_update":
            updated = AppMatch.objects.filter(
                pk=local.pk,
                season_id=local.season_id,
                home_team_id=local.home_team_id,
                away_team_id=local.away_team_id,
                start_time=local.start_time,
                pool_id=local.pool_id,
            ).update(
                home_team_id=home,
                away_team_id=away,
                start_time=row.starts_at,
                pool_id=pool_id,
            )
            if not updated:
                # Re-read the failed CAS before deciding score compatibility.
                local.refresh_from_db()
                self.conflict("match", row.pk, "protected_native_change")
                return False
            local.home_team_id, local.away_team_id = home, away
            local.start_time, local.pool_id = row.starts_at, pool_id
            self.counts["fixtures_updated"] += 1
        # Equality proves the initial baseline only for fixtures we created.
        if row.local_created:
            row.published_schedule = {**row.published_schedule, "fixture": desired}
        return True

    def schedule(self, row: Match, *, accepted: bool) -> tuple[str, ...]:
        """Version changed schedules without rearming a concurrently claimed event.

        Raises:
            RuntimeError: A repair-only publisher reached fixture publication.

        """
        schedule = {
            "starts_at": row.starts_at.astimezone(UTC).isoformat(),
            "status": row.status,
        }
        previous = {
            key: row.published_schedule.get(key) for key in ("starts_at", "status")
        }
        same_time = (
            previous["starts_at"] is not None
            and datetime.fromisoformat(previous["starts_at"]) == row.starts_at
        )
        if same_time and schedule["status"] == previous["status"]:
            return ("published_schedule",)
        row.schedule_notification_id = None
        if (
            accepted
            and row.local_created
            and schedule_changed(row.published_schedule, schedule)
        ):
            if self.schedule_changes is None:
                raise RuntimeError("Fixture publication needs a schedule dispatcher")
            row.schedule_notification_id = uuid4()
            self.schedule_changes(
                notification_id=str(row.schedule_notification_id),
                match_id=str(row.local_match_id),
                starts_at=schedule["starts_at"],
                cancelled=row.status == "CANCELLED",
            )
        row.published_schedule = {**row.published_schedule, **schedule}
        return ("published_schedule", "schedule_notification_id")

    def result(self, row: Match, tracker: MatchData, pool_id: UUID | None) -> bool:
        """Adopt untouched fixtures and stop on local tracking or manual edits."""
        if (
            tracker.live_revision
            or tracker.command_sequence
            or tracker.event_sequence
            or tracker.status == "active"
        ):
            return False
        current = {
            "status": tracker.status,
            "home": tracker.home_score,
            "away": tracker.away_score,
        }
        if row.published_state and current != row.published_state:
            self.conflict("match", row.pk, "local_score_changed")
            return False
        archive = row.external_id.startswith("archive:")
        if archive and not row.local_created:
            return False
        if tracker.score_source != "knkv" and not row.local_created:
            pristine = tracker.status == "upcoming" and not (
                tracker.home_score or tracker.away_score
            )
            if (
                not pristine
                or Shot.objects.filter(match_data=tracker).exists()
                or MatchPart.objects.filter(match_data=tracker).exists()
            ):
                return False
        final = (
            row.status == "FINAL"
            and row.home_score is not None
            and row.away_score is not None
        )
        row.published_state = {
            "status": "finished" if final else "upcoming",
            "home": row.home_score if final else 0,
            "away": row.away_score if final else 0,
        }
        MatchData.objects.filter(pk=tracker.pk).update(
            score_source="archive" if archive else "knkv",
            status=row.published_state["status"],
            home_score=row.published_state["home"],
            away_score=row.published_state["away"],
        )
        self.counts["matches_updated"] += 1

        return True


@dataclass(frozen=True)
class MatchBounds:
    """Bound one publication pass; pending fixtures wait for the next pass."""

    limit: int | None = None
    seasons: set[UUID] | None = None
    # time.monotonic() value after which remaining fixtures are deferred.
    deadline: float | None = None
    # Sweep cursor: only fixtures after this source ID, so fixtures that stay
    # pending (unresolved conflicts) cannot starve the rest of a chunked sweep.
    after: int | None = None


# KNKV keeps a suspended, cancelled or postponed record beside the record that
# settled the same fixture (same teams and kickoff); the final record wins.
SUPERSEDED_STATUSES = frozenset({"SUSPENDED", "CANCELLED", "POSTPONED"})


def final_twin() -> Exists:
    """Match a linked final record of the same fixture as the outer source row."""
    return Exists(
        Match.objects.filter(
            season_id=OuterRef("season_id"),
            home_team_id=OuterRef("home_team_id"),
            away_team_id=OuterRef("away_team_id"),
            starts_at=OuterRef("starts_at"),
            status="FINAL",
            local_match__isnull=False,
        ).exclude(pk=OuterRef("pk"))
    )


def pending_matches(seasons: set[UUID] | None = None) -> QuerySet[Match]:
    """Source fixtures that are unpublished or changed since publication.

    An unlinked superseded record whose fixture belongs to its final twin is done.
    """
    rows = Match.objects.filter(
        Q(local_match=None) | Q(published_at=None) | Q(updated_at__gt=F("published_at"))
    ).exclude(Q(local_match=None, status__in=SUPERSEDED_STATUSES) & final_twin())
    return rows if seasons is None else rows.filter(season_id__in=seasons)


def pending_rows(bounds: MatchBounds) -> list[Match]:
    """Select one bounded pass of pending fixtures, oldest first."""
    query = pending_matches(bounds.seasons).select_related(
        "local_match", "pool", *RULE_RELATIONS
    )
    if bounds.after is not None:
        query = query.filter(pk__gt=bounds.after)
    query = query.order_by("pk")
    return list(query if bounds.limit is None else query[: bounds.limit])


def serialize_with_publication() -> None:
    """Lock both publication lock rows so a caller waits for any running pass.

    Inside a transaction, a repair that also writes fixtures and trackers then
    never holds row locks a concurrent pass needs. Each pass takes only one of
    these rows, so acquiring both in this order cannot form a cycle.
    """
    for key in ("publication", "sportlink"):
        SyncLease.objects.get_or_create(
            key=key, defaults={"expires_at": timezone.now()}
        )
        SyncLease.objects.select_for_update().get(key=key)


@transaction.atomic
def publish_catalogue(
    *,
    schedule_changes: ScheduleChangeDispatcher,
    lease_owner: UUID | None = None,
    overrides: dict[tuple[str, int], str] | None = None,
    bounds: MatchBounds | None = None,
    alongside_import: bool = False,
) -> dict[str, Any]:
    """Materialize snapshots into native models without issuing provider requests.

    ``alongside_import`` publishes while the provider manager imports: passes
    serialize on their own ``publication`` lock row instead of the provider lease
    (whose row every request updates), and a fixture is published as of the change
    it read, so a concurrent import keeps it pending.

    Raises:
        ValueError: Another importer owns the provider lease.

    """
    if alongside_import:
        SyncLease.objects.get_or_create(
            key="publication", defaults={"expires_at": timezone.now()}
        )
        SyncLease.objects.select_for_update().get(key="publication")
    else:
        lease, _ = SyncLease.objects.select_for_update().get_or_create(
            key="sportlink", defaults={"expires_at": timezone.now()}
        )
        if (
            lease.owner is not None
            and lease.expires_at > timezone.now()
            and lease.owner != lease_owner
        ):
            raise ValueError("An import is running; publish after its current batch")
    merged_groups = merge_unlinked_joint_groups(
        protected_ids={pk for (kind, pk) in (overrides or {}) if kind == "team"}
    )
    decisions = Reconciler(
        overrides or {}, lock=True, incremental=True, skip_locked=alongside_import
    ).plan()
    for decision in decisions:
        if decision.reason in {"unique", "explicit"}:
            SOURCE_MODELS[decision.kind].objects.filter(
                pk=decision.source_id
            ).update(**{LOCAL_FIELDS[decision.kind] + "_id": decision.local_id})
    publisher = Publisher(schedule_changes)
    if merged_groups:
        publisher.counts["source_groups_merged"] = merged_groups
    publisher.clubs()
    for source_club in (
        Club.objects
        .exclude(local_club=None)
        .exclude(cached_logo="")
        .select_related("local_club")
    ):
        publish_logo(source_club)
    publisher.teams()
    publisher.pools()
    publisher.matches(bounds)
    publish_pending_rosters(publisher.participation_groups)
    return {
        "counts": dict(publisher.counts),
        "blocked": publisher.blocked,
        "links": dict(Counter(decision.reason for decision in decisions)),
        "last_match": publisher.last_match,
    }
