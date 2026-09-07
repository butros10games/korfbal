"""Publish provider snapshots into the same identities used by native app records."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q
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
)
from apps.competition.services.identities import (
    merge_unlinked_joint_groups,
    unnamed_pool_label,
)
from apps.competition.services.logos import publish_logo
from apps.competition.services.reconciliation import (
    LOCAL_FIELDS,
    SOURCE_MODELS,
    JointTeamIndex,
    Reconciler,
    normalized,
    team_label,
)
from apps.game_tracker.models import MatchData, MatchPart, Shot
from apps.schedule.models import (
    Match as AppMatch,
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


def pool_memberships() -> dict[int, set[Any]]:
    """Collect membership from both published poules and observed fixtures."""
    teams = dict(Team.objects.values_list("pk", "group__local_team_id"))
    members: dict[int, set[Any]] = defaultdict(set)
    for pool, team in PoolEntry.objects.values_list("pool_id", "team_id"):
        if teams.get(team):
            members[pool].add(teams[team])
    for pool, home, away in Match.objects.exclude(pool=None).values_list(
        "pool_id", "home_team_id", "away_team_id"
    ):
        for team_id in (home, away):
            if teams.get(team_id):
                members[pool].add(teams[team_id])
    return members


class Publisher:
    """Resolve and create identities in dependency order using shared lookup indexes."""

    def __init__(self) -> None:
        """Collect publication counts and reviewable conflicts."""
        self.counts: Counter[str] = Counter()
        self.blocked: list[dict[str, Any]] = []

    def conflict(self, kind: str, source_id: int, reason: str) -> None:
        """Retain unresolved identities for explicit review instead of guessing."""
        self.blocked.append({"kind": kind, "source_id": source_id, "reason": reason})

    def clubs(self) -> None:
        """Reuse reviewed club links and create only unclaimed unique names."""
        local_names: dict[str, list[AppClub]] = defaultdict(list)
        for club in AppClub.objects.all():
            local_names[normalized(club.name)].append(club)
        sources = list(Club.objects.all())
        source_names = Counter(normalized(row.name) for row in sources)
        claimed = {row.local_club_id for row in sources if row.local_club_id}
        for row in sources:
            if row.local_club_id:
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

    def teams(self) -> None:
        """Use a global Team plus exactly one TeamData per team and season."""
        sources = list(
            TeamGroup.objects
            .filter(local_team_data__isnull=True)
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
            (str(row.season_id), str(row.local_team_id)): row.pk
            for row in TeamGroup.objects.exclude(local_team=None)
            if row.local_team_id
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

    def pools(self) -> None:
        """Publish poules once and share membership through global teams."""
        members = pool_memberships()
        claimed = set(
            Pool.objects.exclude(local_pool=None).values_list(
                "local_pool_id", flat=True
            )
        )
        through = SeasonPool.teams.through
        existing_members = set(through.objects.values_list("seasonpool_id", "team_id"))
        additions = []
        for row in Pool.objects.select_related("local_pool"):
            local = row.local_pool
            fallback = unnamed_pool_label(row.external_id)
            name = f"{row.class_name} {row.name}".strip() or fallback
            if (
                local is not None
                and local.name in {"", fallback}
                and local.name != name
                and not SeasonPool.objects
                .filter(season_id=row.season_id, name=name, sport=local.sport)
                .exclude(pk=local.pk)
                .exists()
            ):
                local.name = name
                local.save(update_fields=("name",))
            if local is None:
                local, created = SeasonPool.objects.get_or_create(
                    season_id=row.season_id, name=name, sport=row.sport
                )
                if not created and local.pk in claimed:
                    suffix = f" [KNKV {row.external_id}]"
                    name = name[: 512 - len(suffix)] + suffix
                    local, created = SeasonPool.objects.get_or_create(
                        season_id=row.season_id, name=name, sport=row.sport
                    )
                    if not created and local.pk in claimed:
                        self.conflict("pool", row.pk, "pool_already_claimed")
                        continue
                self.counts["pools_created"] += int(created)
                row.local_pool = local
                row.save(update_fields=("local_pool",))
                claimed.add(local.pk)
            for team_id in members[row.pk]:
                pair = (local.pk, team_id)
                if pair not in existing_members:
                    additions.append(through(seasonpool_id=local.pk, team_id=team_id))
                    existing_members.add(pair)
        through.objects.bulk_create(additions, ignore_conflicts=True, batch_size=1000)

    def matches(self) -> None:
        """Publish fixtures and results while keeping tracked history authoritative."""
        rows = list(
            Match.objects.filter(
                Q(local_match=None)
                | Q(published_at=None)
                | Q(updated_at__gt=F("published_at"))
            ).order_by("pk")
        )
        if not rows:
            return
        teams = dict(Team.objects.values_list("pk", "group__local_team_id"))
        pools = dict(Pool.objects.values_list("pk", "local_pool_id"))
        candidates: dict[tuple[Any, ...], list[AppMatch]] = defaultdict(list)
        unlinked = any(row.local_match_id is None for row in rows)
        for match in AppMatch.objects.all() if unlinked else ():
            candidates[
                match.season_id,
                match.home_team_id,
                match.away_team_id,
                match.start_time,
            ].append(match)
        claimed = (
            set(
                Match.objects.exclude(local_match=None).values_list(
                    "local_match_id", flat=True
                )
            )
            if unlinked
            else set()
        )
        for row in rows:
            home, away = teams.get(row.home_team_id), teams.get(row.away_team_id)
            if home is None or away is None:
                self.conflict("match", row.pk, "team_unresolved")
                continue
            if row.local_match_id is None:
                key = (row.season_id, home, away, row.starts_at)
                existing = candidates[key]
                if len(existing) > 1 or (existing and existing[0].pk in claimed):
                    self.conflict("match", row.pk, "ambiguous_fixture")
                    continue
                if existing:
                    local = existing[0]
                else:
                    local = AppMatch.objects.create(
                        season_id=row.season_id,
                        home_team_id=home,
                        away_team_id=away,
                        pool_id=pools.get(row.pool_id),
                        start_time=row.starts_at,
                    )
                    row.local_created = True
                    candidates[key].append(local)
                    self.counts["matches_created"] += 1
                row.local_match = local
                claimed.add(local.pk)
            tracker = MatchData.objects.select_for_update().get(
                match_link_id=row.local_match_id
            )
            self.result(row, tracker, pools.get(row.pool_id))
            row.published_at = timezone.now()
            row.save(
                update_fields=(
                    "local_match",
                    "local_created",
                    "published_at",
                    "published_state",
                )
            )

    def result(self, row: Match, tracker: MatchData, pool_id: UUID | None) -> None:
        """Adopt untouched fixtures and stop on local tracking or manual edits."""
        if (
            tracker.live_revision
            or tracker.command_sequence
            or tracker.event_sequence
            or tracker.status == "active"
        ):
            return
        current = {
            "status": tracker.status,
            "home": tracker.home_score,
            "away": tracker.away_score,
        }
        if row.published_state and current != row.published_state:
            self.conflict("match", row.pk, "local_score_changed")
            return
        if tracker.score_source != "knkv" and not row.local_created:
            pristine = tracker.status == "upcoming" and not (
                tracker.home_score or tracker.away_score
            )
            if (
                not pristine
                or Shot.objects.filter(match_data=tracker).exists()
                or MatchPart.objects.filter(match_data=tracker).exists()
            ):
                return
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
            score_source="knkv",
            status=row.published_state["status"],
            home_score=row.published_state["home"],
            away_score=row.published_state["away"],
        )
        if row.local_created:
            AppMatch.objects.filter(pk=row.local_match_id).update(
                start_time=row.starts_at, pool_id=pool_id
            )
        self.counts["matches_updated"] += 1


@transaction.atomic
def publish_catalogue(
    *,
    lease_owner: UUID | None = None,
    overrides: dict[tuple[str, int], str] | None = None,
) -> dict[str, Any]:
    """Materialize snapshots into native models without issuing provider requests.

    Raises:
        ValueError: Another importer owns the provider lease.

    """
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
    decisions = Reconciler(overrides or {}, lock=True).plan()
    for decision in decisions:
        if decision.reason in {"unique", "explicit"}:
            SOURCE_MODELS[decision.kind].objects.filter(
                pk=decision.source_id
            ).update(**{LOCAL_FIELDS[decision.kind] + "_id": decision.local_id})
    publisher = Publisher()
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
    publisher.matches()
    return {
        "counts": dict(publisher.counts),
        "blocked": publisher.blocked,
        "links": dict(Counter(decision.reason for decision in decisions)),
    }
