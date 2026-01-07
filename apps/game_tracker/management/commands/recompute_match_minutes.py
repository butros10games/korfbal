"""Recompute and persist minutes-played for one or more matches.

This command is intended as an operational backfill:
- if Celery wasn't running (or tasks failed), `PlayerMatchMinutes` rows may be
  missing for finished matches
- team/season pages read only persisted minutes rows and will show `null` when
  minutes are missing

Use this command to recompute minutes for a specific match or bulk over matches.
"""

from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable

from django.core.management.base import BaseCommand
from django.db.models import QuerySet

from apps.game_tracker.models import MatchData
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.game_tracker.services.match_minutes import (
    compute_minutes_by_player_id,
    persist_match_minutes,
)


def _parse_limit(options: dict[str, object]) -> int:
    limit_opt = options.get("limit")
    if isinstance(limit_opt, int):
        return max(0, limit_opt)
    if isinstance(limit_opt, str) and limit_opt.strip():
        return max(0, int(limit_opt))
    return 0


def _build_matchdata_queryset(
    *,
    match_data_id: object,
    finished: bool,
    only_missing: bool,
) -> QuerySet[MatchData]:
    qs = MatchData.objects.select_related("match_link")
    if match_data_id:
        qs = qs.filter(id_uuid=match_data_id)
    if finished:
        qs = qs.filter(status="finished")
    if only_missing:
        qs = qs.exclude(
            player_minutes__algorithm_version=LATEST_MATCH_MINUTES_VERSION,
        ).distinct()
    return qs


def _process_match(
    *,
    md: MatchData,
    dry_run: bool,
    write: Callable[[str], object],
) -> int:
    if dry_run:
        minutes_by_player_id = compute_minutes_by_player_id(match_data=md)
        would_write = sum(1 for minutes in minutes_by_player_id.values() if minutes > 0)
        write(
            f"{md.id_uuid}: would upsert {would_write} rows "
            f"(computed {len(minutes_by_player_id)} players)"
        )
        return 0

    rows = persist_match_minutes(match_data=md)
    write(f"{md.id_uuid}: {rows} rows")
    return rows


class Command(BaseCommand):
    """Django management command to recompute persisted match minutes rows."""

    help = (
        "Recompute PlayerMatchMinutes rows for a match (or all finished matches). "
        "Useful for backfilling when Celery wasn't running."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register CLI arguments for this command."""
        parser.add_argument(
            "--match-data-id",
            dest="match_data_id",
            help="MatchData UUID",
        )
        parser.add_argument(
            "--finished",
            action="store_true",
            help="Recompute all finished matches",
        )
        parser.add_argument(
            "--only-missing",
            action="store_true",
            help=(
                "Only recompute matches missing any persisted minutes at the latest "
                f"algorithm version ({LATEST_MATCH_MINUTES_VERSION})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Don't write anything; only compute how many rows would be upserted "
                "(minutes > 0)."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional limit on number of matches processed (0 = no limit)",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the recomputation for the requested match set."""
        match_data_id = options.get("match_data_id")
        finished = bool(options.get("finished"))
        only_missing = bool(options.get("only_missing"))
        dry_run = bool(options.get("dry_run"))
        limit = _parse_limit(options)

        if not match_data_id and not finished:
            self.stderr.write("Provide --match-data-id or --finished")
            return

        # Match-level filter: if a match has *any* minutes rows at the latest
        # version, we consider it "not missing" for the purpose of this backfill.
        # (If minutes are partially missing for a match, use --finished without
        # --only-missing to force recomputation.)
        qs = _build_matchdata_queryset(
            match_data_id=match_data_id,
            finished=finished,
            only_missing=only_missing,
        )

        processed = 0
        total_rows_written = 0

        for md in qs.iterator():
            if limit and processed >= limit:
                break

            rows = _process_match(md=md, dry_run=dry_run, write=self.stdout.write)

            processed += 1
            total_rows_written += rows

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"Done. Processed {processed} matches.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Processed {processed} matches; "
                    f"upserted {total_rows_written} rows."
                )
            )
