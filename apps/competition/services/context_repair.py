"""Preview and apply competition-context repairs for one provider scope.

The default is a read-only plan. Applying requires an explicit scope and is
bounded (``limit``) and resumable (``after``): each poule is repaired in its
own transaction under the provider and publication leases, so a run can stop
and continue without repeating finished work. Re-running is idempotent.

Native match, team and player UUIDs never change; source evidence is kept;
manually configured or tracked matches only receive reviewed corrections.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.competition.domain.classification import classify, ladder_context
from apps.competition.domain.match_rules import RuleContext, resolve_rules
from apps.competition.models import (
    Allocation,
    Match,
    Pool,
    SeasonBinding,
    SyncLease,
    Team,
    TeamParticipation,
    TeamRating,
)
from apps.competition.services.classification import class_context, map_pool, plan_pool
from apps.competition.services.competition_periods import (
    decide_pool_period,
    resolve_pool_periods,
    season_phase_mismatch,
)
from apps.competition.services.match_rules import (
    RULE_RELATIONS,
    SPORT_DISCIPLINES,
    source_rules,
)
from apps.competition.services.rosters import publish_roster
from apps.competition.services.seasons import (
    OUTDOOR,
    OUTDOOR_PHASES,
    configure_seasons,
    target_season,
)
from apps.game_tracker.models import MatchData, PlayerMatchImpact, PlayerMatchMinutes
from apps.game_tracker.services.match_rule_profiles import (
    APPLIED,
    PENDING_REVIEW,
    accept_reviewed_rules,
    apply_rule_profile,
    tracking_started,
)
from apps.schedule.domain.competition_context import FULL_SEASON
from apps.schedule.models import (
    Match as NativeMatch,
    Season,
    SeasonPool,
)
from apps.schedule.queries.seasons import season_edition
from apps.team.models import TeamData


MAX_REPAIR_BATCH = 5000


# Preview target for a playing season that applying the split would create.
PLANNED_SEASON = UUID(int=0)


@dataclass
class RepairOptions:
    """Explicit repair selection; nothing is written unless ``apply`` is set."""

    scope: Season
    apply: bool = False
    split_outdoor: bool = False
    timing: bool = True
    accept_reviewed: frozenset[UUID] = frozenset()
    limit: int = 200
    after: int = 0
    # Publishes a match's new live revision; bound by the composition root.
    record_change: Callable[[MatchData], object] | None = None


@dataclass
class RepairReport:
    """Aggregate, person-free repair evidence."""

    scope: str
    applied: bool
    counts: Counter[str] = field(default_factory=Counter)
    pools: list[dict[str, Any]] = field(default_factory=list)
    timing: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    next_after: int | None = None

    def as_payload(self) -> dict[str, Any]:
        """Serialize for the management command."""
        return {
            "scope": self.scope,
            "applied": self.applied,
            "counts": dict(sorted(self.counts.items())),
            "pools": self.pools,
            "timing": self.timing,
            "blocked": self.blocked,
            "unresolved": self.unresolved,
            "next_after": self.next_after,
        }


def run(options: RepairOptions) -> RepairReport:
    """Plan, and with ``apply`` perform, one bounded batch of repairs.

    Returns:
        What changed (or would change) and what needs review.

    Raises:
        ValueError: The batch is invalid or an import holds the provider lease.

    """
    if not 0 < options.limit <= MAX_REPAIR_BATCH or options.after < 0:
        raise ValueError("Repair batch requires limit 1-5000 and a nonnegative cursor")
    scope = options.scope
    edition = season_edition(scope)
    report = RepairReport(scope=str(scope.pk), applied=options.apply)
    if edition is None:
        report.unresolved.append({"kind": "season", "reason": "unresolved_edition"})
        return report
    if options.apply:
        _require_idle_importer()
        if options.split_outdoor:
            configure_seasons(scope, scope.start_date.year, split_outdoor=True)
    targets = _phase_targets(scope, edition, split=options.split_outdoor)
    pools = list(
        Pool.objects
        .filter(season=scope, pk__gt=options.after)
        .select_related("season", "local_pool", "competition_class__edition__season")
        .order_by("pk")[: options.limit]
    )
    for pool in pools:
        if options.apply:
            with transaction.atomic():
                _lock_publication()
                _repair_pool(pool, edition, targets, options, report)
        else:
            _repair_pool(pool, edition, targets, options, report)
    report.next_after = pools[-1].pk if len(pools) == options.limit else None
    if options.timing:
        _timing(
            scope,
            [pool.pk for pool in pools],
            include_unpooled=False,
            options=options,
            report=report,
        )
    _accept_reviewed(options, report)
    return report


def _require_idle_importer() -> None:
    """Refuse to repair while an import owns the provider lease.

    Raises:
        ValueError: An import currently owns the provider lease.

    """
    now = timezone.now()
    for key in ("publication", "sportlink"):
        SyncLease.objects.get_or_create(key=key, defaults={"expires_at": now})
    lease = SyncLease.objects.get(key="sportlink")
    if lease.owner is not None and lease.expires_at > now:
        raise ValueError("Stop the importer and wait for its lease before repair")


def _lock_publication() -> None:
    """Serialize each repair transaction with both publication paths.

    ``publish_catalogue`` holds the ``publication`` row beside an import and the
    ``sportlink`` row otherwise; taking them in this fixed order cannot cycle.

    Raises:
        ValueError: An active importer owns a publication lease.

    """
    leases = list(
        SyncLease.objects
        .select_for_update()
        .filter(key__in=("publication", "sportlink"))
        .order_by("key")
    )
    now = timezone.now()
    if any(lease.owner is not None and lease.expires_at > now for lease in leases):
        raise ValueError("Stop the importer and wait for its lease before repair")


def _phase_targets(scope: Season, edition: int, *, split: bool) -> dict[str, UUID]:
    """Return the native season each outdoor phase routes to (planned or bound)."""
    bound = {
        row.phase: row.season_id
        for row in SeasonBinding.objects.filter(scope=scope, sport=OUTDOOR)
    }
    if bound and any(phase in bound for phase in OUTDOOR_PHASES):
        return {
            phase: bound.get(phase, bound.get("", scope.pk)) for phase in OUTDOOR_PHASES
        }
    if not split:
        default = bound.get("", scope.pk)
        return dict.fromkeys(OUTDOOR_PHASES, default)
    # Preview of a split that is not configured yet: existing halves by stored
    # context, or a placeholder for a season the apply run would create.
    planned = {
        season.phase: season.pk
        for season in Season.objects.filter(
            edition=edition, phase__in=("autumn", "spring")
        )
    }
    return {
        "autumn": planned.get("autumn", PLANNED_SEASON),
        "spring": planned.get("spring", PLANNED_SEASON),
        "full_season": scope.pk,
    }


def _repair_pool(
    pool: Pool,
    edition: int,
    targets: dict[str, UUID],
    options: RepairOptions,
    report: RepairReport,
) -> None:
    """Decide one poule's period and move its fixtures to that period's season."""
    if options.apply:
        pool = (
            Pool.objects
            .select_for_update(of=("self",), no_key=True)
            .select_related(
                "season", "local_pool", "competition_class__edition__season"
            )
            .get(pk=pool.pk)
        )
    days = [
        timezone.localdate(stamp)
        for stamp in Match.objects.filter(pool=pool).values_list("starts_at", flat=True)
    ]
    decision = decide_pool_period(
        sport=pool.sport, class_name=pool.class_name, edition=edition, days=days
    )
    phase = decision.phase or ""
    entry: dict[str, Any] = {
        "pool": pool.pk,
        "stored_phase": pool.phase or None,
        "proposed_phase": phase or None,
        "evidence": decision.evidence,
    }
    report.counts["pools_seen"] += 1
    report.pools.append(entry)
    _classification_impact(pool, phase, entry, report)
    if not phase:
        report.unresolved.append({"kind": "pool", "id": pool.pk, **decision.evidence})
        report.counts["pools_unresolved"] += 1
        return
    if season_phase_mismatch(pool, phase):
        entry["conflict"] = "season_phase_mismatch"
        entry["routing_review"] = "full_year_repair"
        report.blocked.append({
            "kind": "pool",
            "id": pool.pk,
            "reason": entry["conflict"],
        })
        report.counts["season_phase_mismatch"] += 1
        return
    correction = bool(pool.phase) and pool.phase != phase
    if correction and (pool.sport != OUTDOOR or phase not in OUTDOOR_PHASES):
        entry["conflict"] = "stored_phase_differs"
        report.blocked.append({
            "kind": "pool",
            "id": pool.pk,
            "reason": entry["conflict"],
        })
        return
    if correction:
        # The poule's fixtures now show another period, typically an autumn
        # decision taken before the spring schedule was published.
        entry["phase_change"] = {"from": pool.phase, "to": phase}
        report.counts["period_corrections"] += 1
    target = targets.get(phase) if pool.sport == OUTDOOR else None
    moved = (
        target is None
        or phase not in OUTDOOR_PHASES
        or _move_fixtures(pool, (phase, target), options, report, entry)
    )
    if not moved:
        entry["conflict"] = "routing_blocked"
        return
    if not options.apply:
        # A blocked poule keeps its stored period: recording one would route its
        # source rows away from the native fixtures that stayed put.
        return
    if correction:
        _correct_period(pool, phase, decision.evidence, report)
    elif not pool.phase:
        periods = resolve_pool_periods([pool.pk])
        report.counts["allocations_realigned"] += periods.get(
            "allocations_realigned", 0
        )
        report.counts["pool_periods_recorded"] += 1
    result = map_pool(pool)
    report.counts["classifications_applied"] += int(result["changed"])
    report.counts["allocations_realigned"] += result["allocations_realigned"]


def _classification_impact(
    pool: Pool, phase: str, entry: dict[str, Any], report: RepairReport
) -> None:
    """Preview dependent effects using immutable class keys, with no writes."""
    old = pool.competition_class
    before = asdict(class_context(old)) if old else None
    after = plan_pool(pool, phase=phase) if phase else plan_pool(pool)
    resolved = after["status"] not in {"conflict", "unresolved"}
    proposed = after["classification"] if resolved else None
    key_changed = before != proposed
    old_category = old.category if old else None
    category = proposed["category"] if proposed else None
    changed = key_changed or (old.level if old else None) != after["level"]
    entry["classification"] = {
        "before": {
            "class": old.pk if old else None,
            "key": before,
            "level": old.level if old else None,
            "status": pool.mapping_status,
        },
        "after": after,
        "key_changed": key_changed,
        "changed": changed,
    }
    report.counts["classifications_to_change"] += int(changed)
    report.counts["category_a_enter"] += int(old_category != "a" and category == "a")
    report.counts["category_a_exit"] += int(old_category == "a" and category != "a")
    allocations = []
    for row in Allocation.objects.filter(entry__pool=pool).select_related(
        "source__season", "competition_class__edition"
    ):
        value, issues = classify(
            "",
            "",
            season_edition(row.source.season),
            {**row.classification, **({"phase": phase} if phase else {})},
        )
        metadata = ladder_context(value, issues, season_edition(row.source.season))
        target = target_season(row.source.season, pool.sport, phase)
        old_key = (
            asdict(class_context(row.competition_class))
            if row.competition_class
            else None
        )
        new_key = asdict(value)
        differs = old_key != new_key or (
            row.competition_class
            and target
            and row.competition_class.edition.season_id != target.pk
        )
        report.counts["allocations_to_realign"] += int(bool(differs))
        exclusion = (
            "unresolved_context"
            if not resolved or new_key != proposed
            else "missing_baseline"
            if category == "b" and row.knkv_points is None
            else ""
        )
        allocations.append({
            "allocation": row.pk,
            "before": old_key,
            "after": new_key,
            "key_changed": bool(differs),
            "issues": issues,
            **metadata,
            "rating_exclusion": exclusion,
            "average_age": str(row.average_age)
            if row.average_age is not None
            else None,
            "knkv_points": str(row.knkv_points)
            if row.knkv_points is not None
            else None,
        })
    entry["allocations"] = allocations
    entry["impacts"] = {
        "category": {"before": old_category, "after": category},
        "automatic_substitutions": {
            "before": old_category == "a",
            "after": category == "a",
            "requires_team_opt_in": True,
        },
        "polling_duration_class_changed": key_changed,
        "rating_rows_using_class": TeamRating.objects.filter(
            competition_class=old
        ).count()
        if old
        else 0,
        "published_rating_cache": "content_fingerprint",
        "rating_class_review_required": key_changed,
    }


def _correct_period(
    pool: Pool, phase: str, evidence: dict[str, Any], report: RepairReport
) -> None:
    """Record a reviewed period correction once its fixtures were moved.

    Participations the old period gave only through this poule are removed; their
    native TeamData rows (and manually added people) remain.
    """
    previous = pool.phase
    Pool.objects.filter(pk=pool.pk).update(
        phase=phase,
        phase_evidence={
            "decision": evidence,
            "corrected_from": previous,
            "corrected_at": timezone.now().isoformat(),
        },
    )
    pool.phase = phase
    teams = _pool_teams(Match.objects.filter(pool=pool))
    still_playing = _pool_teams(
        Match.objects.filter(
            pool__season_id=pool.season_id, pool__phase=previous
        ).exclude(pool=pool)
    )
    stale = TeamParticipation.objects.filter(
        team_id__in=teams - still_playing, phase=previous
    )
    groups = set(stale.values_list("team__group_id", flat=True))
    report.counts["participations_removed"] += stale.delete()[0]
    for group in groups - {None}:
        team = Team.objects.filter(group_id=group).first()
        if team is not None:
            publish_roster(team)
    report.counts["periods_corrected"] += 1


def _pool_teams(matches: QuerySet[Match]) -> set[int]:
    """Return the source teams playing the given fixtures."""
    return set(matches.values_list("home_team_id", flat=True)) | set(
        matches.values_list("away_team_id", flat=True)
    )


def _move_fixtures(
    pool: Pool,
    destination: tuple[str, UUID],
    options: RepairOptions,
    report: RepairReport,
    entry: dict[str, Any],
) -> bool:
    """Move a poule's published fixtures to its period's native season.

    Returns:
        Whether every fixture is (or, in a preview, would be) in that season.

    """
    phase, target = destination
    rows = list(
        Match.objects
        .filter(pool=pool, local_match__isnull=False)
        .select_related("local_match", "home_team", "away_team")
        .order_by("pk")
    )
    moving = [row for row in rows if row.local_match.season_id != target]
    if not moving:
        return True
    local_pool = pool.local_pool
    unrelated = (
        NativeMatch.objects
        .filter(pool=local_pool)
        .exclude(pk__in=[row.local_match_id for row in rows])
        .exists()
        if local_pool
        else False
    )
    blocked = [row for row in moving if not row.local_created]
    # Checked before any write: a blocked poule must not move part of itself.
    name_taken = (
        local_pool is not None
        and local_pool.season_id != target
        and SeasonPool.objects
        .filter(season_id=target, name=local_pool.name, sport=local_pool.sport)
        .exclude(pk=local_pool.pk)
        .exists()
    )
    if unrelated or blocked or name_taken:
        reason = (
            "native_pool_shared"
            if unrelated
            else "native_pool_name_taken"
            if name_taken
            else "native_match_review"
        )
        report.blocked.append({"kind": "pool", "id": pool.pk, "reason": reason})
        report.counts["pools_blocked"] += 1
        return False
    entry.update(
        target_season=str(target),
        fixtures=len(moving),
        tracked=sum(
            1
            for data in MatchData.objects.filter(
                match_link_id__in=[row.local_match_id for row in moving]
            )
            if tracking_started(data)
        ),
    )
    report.counts["fixtures_to_move"] += len(moving)
    if not options.apply:
        return True
    # Lock order: source fixtures, then native fixtures and the native poule.
    list(
        Match.objects.select_for_update(of=("self",), no_key=True).filter(
            pk__in=[row.pk for row in moving]
        )
    )
    NativeMatch.objects.select_for_update(no_key=True).filter(
        pk__in=[row.local_match_id for row in moving]
    ).update(season_id=target)
    if local_pool and local_pool.season_id != target:
        SeasonPool.objects.filter(pk=local_pool.pk).update(season_id=target)
    teams = {row.home_team_id for row in moving} | {row.away_team_id for row in moving}
    if phase != FULL_SEASON:
        # Continuous poules use the scope's default team seasons.
        _participate(teams, phase, target, report)
    report.counts["fixtures_moved"] += len(moving)
    return True


def _participate(
    team_ids: set[int], phase: str, target: UUID, report: RepairReport
) -> None:
    """Give teams of moved fixtures a roster in the period's native season."""
    groups: set[int] = set()
    for team in Team.objects.filter(pk__in=team_ids).select_related("group"):
        if team.group is None or team.group.local_team_id is None:
            report.blocked.append({
                "kind": "team",
                "id": team.pk,
                "reason": "team_unresolved",
            })
            continue
        data, created = TeamData.objects.get_or_create(
            team_id=team.group.local_team_id, season_id=target
        )
        report.counts["team_seasons_created"] += int(created)
        _, made = TeamParticipation.objects.get_or_create(
            team=team, phase=phase, defaults={"team_data": data}
        )
        report.counts["participations_created"] += int(made)
        groups.add(team.group.pk)
    for group in groups:
        team = Team.objects.filter(group_id=group).first()
        if team is not None:
            publish_roster(team)


def _timing(
    scope: Season,
    pool_ids: list[int],
    *,
    include_unpooled: bool,
    options: RepairOptions,
    report: RepairReport,
) -> None:
    """Compare stored tracker rules with the resolved profile of each fixture.

    Work follows the poule batch, so ``after``/``limit`` bound it as well.
    """
    selected = Q(pool_id__in=pool_ids)
    if include_unpooled:
        selected |= Q(pool__isnull=True)
    rows = (
        Match.objects
        .filter(selected, season=scope, local_match__isnull=False)
        .select_related(*RULE_RELATIONS)
        .order_by("pk")
    )
    proposed = {
        item["pool"]: item["classification"]["after"]
        for item in report.pools
        if "conflict" not in item and item.get("proposed_phase")
    }
    blocked = {item["pool"] for item in report.pools if "conflict" in item}
    for row in rows:
        if row.pool_id in blocked:
            continue
        decision = proposed.get(row.pool_id)
        if not options.apply and decision is not None:
            values = (
                decision["classification"]
                if decision["status"] not in {"conflict", "unresolved"}
                else {}
            )
            context = RuleContext(
                edition=season_edition(row.season),
                **{
                    key: values.get(key)
                    for key in (
                        "discipline",
                        "category",
                        "age_group",
                        "colour",
                        "playing_format",
                        "code",
                        "gender",
                        "team_kind",
                    )
                },
            )
            if context.discipline in {None, "unknown"}:
                context = replace(
                    context, discipline=SPORT_DISCIPLINES.get(row.home_team.sport)
                )
            rules = resolve_rules(
                context,
                minutes=row.playing_time_minutes,
                periods=row.match_periods or (),
            )
        else:
            rules = source_rules(row)
        tracker = MatchData.objects.get(match_link_id=row.local_match_id)
        if tracker.rules == rules.as_snapshot():
            continue
        started = tracking_started(tracker)
        item = {
            "match": str(row.local_match_id),
            "from": tracker.match_rules().regulation_minutes,
            "to": rules.regulation_minutes,
            "source": rules.source,
            "before_rules": tracker.rules,
            "after_rules": rules.as_snapshot(),
            "tracked": started,
            "dependent_minutes": PlayerMatchMinutes.objects.filter(
                match_data=tracker
            ).count(),
            "dependent_impacts": PlayerMatchImpact.objects.filter(
                match_data=tracker
            ).count(),
        }
        if options.apply:
            with transaction.atomic():
                tracker = MatchData.objects.select_for_update(no_key=True).get(
                    pk=tracker.pk
                )
                item["outcome"] = apply_rule_profile(tracker, rules)
        else:
            item["outcome"] = PENDING_REVIEW if started else APPLIED
        report.counts[f"timing_{item['outcome']}"] += 1
        report.timing.append(item)


def _accept_reviewed(options: RepairOptions, report: RepairReport) -> None:
    """Apply reviewed pending profiles and rebuild their derived rows."""
    for match_id in sorted(options.accept_reviewed, key=str):
        if not options.apply or options.record_change is None:
            report.counts["reviewed_to_accept"] += 1
            continue
        counts = accept_reviewed_rules(match_id, record_change=options.record_change)
        if counts is None:
            report.blocked.append({
                "kind": "match",
                "id": str(match_id),
                "reason": "nothing_pending",
            })
            continue
        report.counts.update(counts)
        report.counts["reviewed_accepted"] += 1
