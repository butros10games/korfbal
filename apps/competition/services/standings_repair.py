"""Preview and apply bounded standings repairs, one explicit selection at a time.

Stages:

- ``blank-closed``: closed poules whose official table is entirely empty although
  scored finals exist. Official recovery comes first: a retained official table
  in checkpoint evidence, or an official checkpoint still waiting for the
  historical worker, keeps the poule for the importer. Otherwise a labelled,
  provisional table generated from canonical scored finals is stored beside the
  empty official one. Provider and mixed poules are included deliberately; a
  feed filtered to one club's results gets no generated table.
- ``convert-legacy``: legacy generated rows (``standing`` with ``Computed``) get
  a generated table in ``computed_standing``, recomputed from canonical finals.
  The legacy copy stays readable for older deployments.
- ``clear-legacy``: once every reader understands ``computed_standing``, clear
  the legacy copies of converted poules.

A preview performs no writes and no provider requests. Applying requires the
preview's manifest: each listed poule is re-evaluated in its own transaction
under no-key locks on only that poule and its memberships, and any poule whose
fingerprint changed since the preview is skipped as stale.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.domain.standings_provenance import (
    COMPUTED,
    generated_standing,
    is_legacy_computed,
    is_official_standing,
)
from apps.competition.models import HistoricalResource, Match, Pool, PoolEntry
from apps.competition.services.computed_standings import (
    canonical_results,
    locked_entries,
    plan_table,
    write_generated_table,
)
from apps.competition.services.history_editions import edition_scopes


STAGES = ("blank-closed", "convert-legacy", "clear-legacy")
MAX_LIMIT = 500
# Outcomes that an apply acts on, per stage.
ACTIONS = {
    "blank-closed": "generate",
    "convert-legacy": "convert",
    "clear-legacy": "clear",
}
# Historical checkpoints that can deliver a poule's official table.
OFFICIAL_PROVIDERS = ("app", "dataservice")
OFFICIAL_KINDS = ("edition_pool", "pool", "pool_window", "standing")


@dataclass(frozen=True)
class Selection:
    """An explicit, bounded and resumable selection of poules."""

    stage: str
    editions: tuple[int, ...] = ()
    pool_ids: tuple[int, ...] = ()
    after: int = 0
    limit: int = 20

    def validate(self) -> None:
        """Refuse unscoped or unbounded selections.

        Raises:
            ValueError: The stage, scope, cursor or limit is invalid.

        """
        if self.stage not in STAGES:
            raise ValueError(f"Unknown stage {self.stage}")
        if not self.editions and not self.pool_ids:
            raise ValueError("Select at least one edition or poule")
        if not 0 < self.limit <= MAX_LIMIT or self.after < 0:
            raise ValueError(
                f"Limit must be between 1 and {MAX_LIMIT}; the cursor nonnegative"
            )


@dataclass
class Report:
    """What a preview found, or what an apply changed, per poule."""

    stage: str
    applied: bool
    selection: dict[str, Any]
    pools: list[dict[str, Any]] = field(default_factory=list)
    next_after: int | None = None

    def as_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable manifest."""
        return {
            "stage": self.stage,
            "applied": self.applied,
            "selection": self.selection,
            "counts": dict(
                sorted(Counter(row["outcome"] for row in self.pools).items())
            ),
            "pools": self.pools,
            "next_after": self.next_after,
        }


def _stage_filter(stage: str, today: date) -> Q:
    entries = PoolEntry.objects.all()
    legacy = Q(pk__in=entries.filter(standing__Computed=True).values("pool_id"))
    if stage == "convert-legacy":
        return legacy
    if stage == "clear-legacy":
        return legacy & Q(standings_provenance__has_key=COMPUTED)
    scored = Match.objects.filter(
        status="FINAL",
        home_score__isnull=False,
        away_score__isnull=False,
    )
    return (
        Q(season__end_date__lt=today)
        & Q(pk__in=entries.values("pool_id"))
        & ~Q(pk__in=entries.exclude(standing={}).values("pool_id"))
        & Q(pk__in=scored.exclude(pool=None).values("pool_id"))
    )


def select_pools(selection: Selection, today: date) -> list[Pool]:
    """Return one ordered page of candidates; named poules are always evaluated."""
    scopes = [Q(pk__in=selection.pool_ids)] if selection.pool_ids else []
    if selection.editions:
        seasons = [
            season.pk
            for edition in selection.editions
            for season in edition_scopes(edition)
        ]
        scopes.append(Q(season_id__in=seasons) & _stage_filter(selection.stage, today))
    scope = scopes[0]
    for other in scopes[1:]:
        scope |= other
    return list(
        Pool.objects
        .filter(scope, pk__gt=selection.after)
        .select_related("season")
        .order_by("pk")[: selection.limit]
    )


def official_checkpoints(pools: Iterable[Pool]) -> dict[int, list[dict[str, Any]]]:
    """Return historical checkpoints that can deliver each poule's official table.

    A poule ID can be reused in another edition, so checkpoints must overlap the
    poule's season. Read in one query through the kind index.
    """
    pools = list(pools)
    by_source: dict[str, list[Pool]] = defaultdict(list)
    for pool in pools:
        for source_id in {pool.external_id, pool.external_id.removeprefix("ds:")}:
            by_source[source_id].append(pool)
    found: dict[int, list[dict[str, Any]]] = {pool.pk: [] for pool in pools}
    for checkpoint in (
        HistoricalResource.objects
        .filter(
            kind__in=OFFICIAL_KINDS,
            provider__in=OFFICIAL_PROVIDERS,
            source_id__in=list(by_source),
        )
        .order_by("pk")
        .values(
            "pk",
            "provider",
            "kind",
            "source_id",
            "state",
            "coverage",
            "reason",
            "fetched_at",
            "start_date",
            "end_date",
            "evidence",
        )
    ):
        for pool in by_source[checkpoint["source_id"]]:
            if (
                checkpoint["start_date"] <= pool.season.end_date
                and checkpoint["end_date"] >= pool.season.start_date
            ):
                table = (checkpoint["evidence"] or {}).get("official_table") or {}
                found[pool.pk].append({
                    "id": checkpoint["pk"],
                    "provider": checkpoint["provider"],
                    "kind": checkpoint["kind"],
                    "state": checkpoint["state"],
                    "coverage": checkpoint["coverage"],
                    "reason": checkpoint["reason"],
                    "fetched_at": (
                        checkpoint["fetched_at"].isoformat()
                        if checkpoint["fetched_at"]
                        else None
                    ),
                    "retained_rows": len(table.get("PoolStandingTeam") or []),
                })
    return found


def _fingerprint(
    pool: Pool, entries: list[PoolEntry], checkpoints: list[dict[str, Any]]
) -> str:
    matches = list(
        Match.objects
        .filter(pool=pool)
        .order_by("pk")
        .values_list(
            "pk",
            "external_id",
            "status",
            "home_score",
            "away_score",
            "home_team_id",
            "away_team_id",
            "starts_at",
        )
    )
    payload = {
        "pool": [
            pool.pk,
            pool.class_name,
            pool.results_filtered,
            pool.standings_provenance,
            pool.season.end_date,
        ],
        "entries": [
            [
                entry.pk,
                entry.team_id,
                entry.team.group_id,
                entry.standing,
                entry.computed_standing,
            ]
            for entry in entries
        ],
        "matches": matches,
        "checkpoints": checkpoints,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


def _plan(
    pool: Pool,
    entries: list[PoolEntry],
    checkpoints: list[dict[str, Any]],
    stage: str,
    today: date,
) -> tuple[str, dict[str, Any]]:
    """Decide one poule's outcome without writing anything."""
    blocked = _blocked(pool, entries, checkpoints, stage, today)
    if blocked is not None:
        return blocked
    outcome, details = _table_outcome(pool, entries, stage)
    if stage == "blank-closed":
        details["official_checkpoints"] = checkpoints
    return outcome, details


def _blocked(
    pool: Pool,
    entries: list[PoolEntry],
    checkpoints: list[dict[str, Any]],
    stage: str,
    today: date,
) -> tuple[str, dict[str, Any]] | None:
    """Return why a poule needs no generated table, or None."""
    legacy = [entry for entry in entries if is_legacy_computed(entry.standing)]
    if stage == "clear-legacy":
        converted = COMPUTED in (pool.standings_provenance or {}) and any(
            entry.computed_standing is not None for entry in entries
        )
        outcome = (
            "no_legacy" if not legacy else "clear" if converted else "not_converted"
        )
        return outcome, {"legacy_rows": len(legacy)}
    if any(is_official_standing(entry.standing) for entry in entries):
        return "official_present", {}
    if stage == "convert-legacy":
        return None if legacy else ("no_legacy", {})
    # Official recovery first: a retained or still pending official table wins.
    reasons = (
        (pool.season.end_date >= today, "season_open"),
        (not entries, "no_entries"),
        (bool(legacy), "legacy_table"),
        (pool.results_filtered, "filtered_feed"),
        (any(row["retained_rows"] for row in checkpoints), "official_retained"),
        (any(row["state"] == "pending" for row in checkpoints), "official_pending"),
    )
    outcome = next((reason for applies, reason in reasons if applies), None)
    return None if outcome is None else (outcome, {"official_checkpoints": checkpoints})


def _table_outcome(
    pool: Pool, entries: list[PoolEntry], stage: str
) -> tuple[str, dict[str, Any]]:
    """Compare the table canonical finals generate with the stored one."""
    inputs = canonical_results(pool.pk)
    if not inputs.results:
        return ("unavailable" if stage == "blank-closed" else "no_results"), {}
    values, digest = plan_table(pool, entries, inputs)
    details: dict[str, Any] = {
        "results": len(inputs.results),
        "partial": inputs.partial,
    }
    if stage == "convert-legacy":
        legacy = [entry for entry in entries if is_legacy_computed(entry.standing)]
        details["legacy_rows"] = len(legacy)
        details["legacy_rows_changed"] = sum(
            values.get(entry.pk) != generated_standing(entry.standing, None)
            for entry in legacy
        )
    current = (pool.standings_provenance or {}).get(COMPUTED) or {}
    unchanged = current.get("digest") == digest and all(
        entry.computed_standing == values[entry.pk] for entry in entries
    )
    return ("unchanged" if unchanged else ACTIONS[stage]), details


def _entries(pool: Pool) -> list[PoolEntry]:
    return list(
        PoolEntry.objects.filter(pool=pool).select_related("team").order_by("pk")
    )


def preview(selection: Selection, today: date | None = None) -> Report:
    """Report every selected poule's outcome and fingerprint; writes nothing.

    Returns:
        A manifest for a later apply.

    """
    selection.validate()
    today = today or timezone.localdate()
    pools = select_pools(selection, today)
    checkpoints = official_checkpoints(pools)
    report = Report(
        stage=selection.stage,
        applied=False,
        selection={
            "editions": list(selection.editions),
            "pools": list(selection.pool_ids),
            "after": selection.after,
            "limit": selection.limit,
            "date": today.isoformat(),
        },
        next_after=pools[-1].pk if len(pools) == selection.limit else None,
    )
    for pool in pools:
        entries = _entries(pool)
        outcome, details = _plan(
            pool, entries, checkpoints[pool.pk], selection.stage, today
        )
        report.pools.append({
            "id": pool.pk,
            "external_id": pool.external_id,
            "season": pool.season.name,
            "outcome": outcome,
            "fingerprint": _fingerprint(pool, entries, checkpoints[pool.pk]),
            **details,
        })
    return report


def apply(manifest: dict[str, Any], today: date | None = None) -> Report:
    """Apply a reviewed preview; changed poules are skipped as stale.

    Returns:
        What happened to each actionable poule of the manifest.

    Raises:
        ValueError: The manifest is not an unapplied preview of a known stage.

    """
    stage = manifest.get("stage")
    if stage not in STAGES or manifest.get("applied") is not False:
        raise ValueError("Apply needs an unapplied preview manifest")
    planned = {
        int(row["id"]): row
        for row in manifest.get("pools", [])
        if row.get("outcome") == ACTIONS[stage]
    }
    if len(planned) > MAX_LIMIT:
        raise ValueError(f"A manifest may apply at most {MAX_LIMIT} poules")
    today = today or date.fromisoformat(manifest["selection"]["date"])
    report = Report(stage=stage, applied=True, selection=manifest["selection"])
    pools = list(
        Pool.objects.filter(pk__in=planned).select_related("season").order_by("pk")
    )
    checkpoints = official_checkpoints(pools)
    for pool in pools:
        with transaction.atomic():
            locked = (
                Pool.objects
                .select_for_update(no_key=True)
                .select_related("season")
                .get(pk=pool.pk)
            )
            entries = locked_entries(locked)
            outcome, _ = _plan(locked, entries, checkpoints[pool.pk], stage, today)
            fingerprint = _fingerprint(locked, entries, checkpoints[pool.pk])
            if (
                outcome != ACTIONS[stage]
                or fingerprint != planned[pool.pk]["fingerprint"]
            ):
                result = "stale"
            elif stage == "clear-legacy":
                legacy = [
                    entry for entry in entries if is_legacy_computed(entry.standing)
                ]
                for entry in legacy:
                    entry.standing = {}
                PoolEntry.objects.bulk_update(legacy, ["standing"])
                result = "cleared"
            else:
                result = write_generated_table(
                    locked,
                    entries,
                    reason=(
                        "closed_blank_fallback"
                        if stage == "blank-closed"
                        else "legacy_conversion"
                    ),
                )
        report.pools.append({
            "id": pool.pk,
            "external_id": pool.external_id,
            "outcome": result,
        })
    missing = sorted(set(planned) - {pool.pk for pool in pools})
    report.pools.extend({"id": pk, "outcome": "missing"} for pk in missing)
    return report
