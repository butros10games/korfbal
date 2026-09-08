"""Preview and repair season links using stored observations, without provider calls."""

from collections import Counter
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.competition.models import (
    Allocation,
    Match,
    Pool,
    SyncLease,
    SyncResource,
    Team,
)
from apps.competition.services.allocations import allocation_class
from apps.competition.services.classification import map_pool
from apps.competition.services.publishing import Publisher
from apps.competition.services.rosters import publish_roster
from apps.competition.services.seasons import (
    INDOOR,
    SeasonResolver,
    configure_seasons,
)
from apps.schedule.models import (
    Match as NativeMatch,
    Season,
)


def preview(scope: Season) -> dict[str, Any]:
    """Report affected stored identities without making changes."""
    return {
        "scope": str(scope.pk),
        "teams": dict(
            Counter(Team.objects.filter(season=scope).values_list("sport", flat=True))
        ),
        "poules": dict(
            Counter(Pool.objects.filter(season=scope).values_list("sport", flat=True))
        ),
        "matches": Match.objects.filter(season=scope, home_team__sport=INDOOR).count(),
        "rosters_to_refresh": SyncResource.objects.filter(
            season=scope, kind="team_roster", fetched_at__isnull=False
        ).count(),
    }


@transaction.atomic
def repair(
    scope: Season, year: int, *, refresh_rosters: bool = False
) -> dict[str, Any]:
    """Repair a scoped catalogue atomically, preserving UUIDs and native history.

    Raises:
        ValueError: Stop the importer before changing its publication mappings.

    """
    lease, _ = SyncLease.objects.select_for_update().get_or_create(
        key="sportlink", defaults={"expires_at": timezone.now()}
    )
    if lease.owner is not None and lease.expires_at > timezone.now():
        raise ValueError("Stop the importer and wait for its lease before repair")
    configure_seasons(scope, year)
    resolver = SeasonResolver()
    blocked = []
    changed = Counter()
    pools = Pool.objects.filter(season=scope).select_related("local_pool", "season")
    for row in pools:
        target = resolver.resolve(row.season_id, row.sport)
        if target is None:
            blocked.append({
                "kind": "pool",
                "id": row.pk,
                "reason": "unknown_discipline",
            })
            continue
        local = row.local_pool
        if local and local.season_id != target:
            unrelated = NativeMatch.objects.filter(pool=local).exclude(
                pk__in=Match.objects.filter(pool=row, local_created=True).values(
                    "local_match_id"
                )
            )
            if (
                unrelated.exists()
                or type(local)
                .objects.filter(season_id=target, name=local.name, sport=local.sport)
                .exclude(pk=local.pk)
                .exists()
            ):
                blocked.append({
                    "kind": "pool",
                    "id": row.pk,
                    "reason": "native_pool_conflict",
                })
                continue
            local.season_id = target
            local.save(update_fields=("season",))
            changed["poules"] += 1
        map_pool(row)
    repair_allocations(scope, blocked, changed)
    repair_matches(scope, resolver, blocked, changed)
    publisher = Publisher()
    publisher.team_variants(force=True, scope=scope)
    for group_id in (
        Team.objects.filter(season=scope).values_list("group_id", flat=True).distinct()
    ):
        team = Team.objects.filter(group_id=group_id).first()
        if team:
            publish_roster(team)
    if refresh_rosters:
        # A schema refresh is explicit; failures stay bounded and images stay cached.
        changed["rosters_queued"] = SyncResource.objects.filter(
            season=scope, kind="team_roster", fetched_at__isnull=False, failures=0
        ).update(fetched_at=None, next_sync_at=timezone.now(), etag="")
    return {"changed": dict(changed), "blocked": blocked}


def repair_matches(
    scope: Season, resolver: SeasonResolver, blocked: list[dict], changed: Counter
) -> None:
    """Move only importer-created fixtures with consistent season evidence."""
    for row in Match.objects.filter(season=scope).select_related(
        "home_team", "away_team", "local_match", "pool__local_pool"
    ):
        target = resolver.resolve(row.season_id, row.home_team.sport)
        local = row.local_match
        if target is None or row.home_team.sport != row.away_team.sport:
            blocked.append({
                "kind": "match",
                "id": row.pk,
                "reason": "conflicting_discipline",
            })
            continue
        if not local or local.season_id == target:
            continue
        if not row.local_created or (
            row.pool and row.pool.local_pool and row.pool.local_pool.season_id != target
        ):
            blocked.append({
                "kind": "match",
                "id": row.pk,
                "reason": "native_match_review",
            })
            continue
        local.season_id = target
        local.save(update_fields=("season",))
        changed["matches"] += 1


def repair_allocations(scope: Season, blocked: list[dict], changed: Counter) -> None:
    """Align stored allocation classes without changing their file provenance."""
    classes = {}
    for allocation in Allocation.objects.filter(source__season=scope):
        try:
            class_id = allocation_class(scope, allocation.classification, classes)
        except ValueError:
            blocked.append({
                "kind": "allocation",
                "id": allocation.pk,
                "reason": "classification_review",
            })
            continue
        if allocation.competition_class_id != class_id:
            allocation.competition_class_id = class_id
            allocation.save(update_fields=("competition_class",))
            changed["allocations"] += 1
