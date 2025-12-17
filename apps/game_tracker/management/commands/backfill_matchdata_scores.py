"""Backfill MatchData.home_score/away_score from Shot rows.

Use this after deploying the score persistence fix so historical finished matches
stop showing 0-0.

Example:
    uv run python apps/django_projects/korfbal/manage.py backfill_matchdata_scores

Notes:
    CLI options:
        - --dry-run: don't write changes, just report.
        - --only-missing: only update rows where both stored scores are 0.
        - --batch-size: number of MatchData rows to process per batch.

"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_scores import compute_scores_for_matchdata_ids


@dataclass(frozen=True, slots=True)
class _BatchResult:
    processed: int
    updated: int


def _chunks(values: list[UUID], size: int) -> Iterable[list[UUID]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


class Command(BaseCommand):
    """Backfill persisted MatchData scores for finished matches."""

    help = "Backfill MatchData scores from shots for finished matches."

    def add_arguments(self, parser: Any) -> None:  # noqa: ANN401
        """Register CLI arguments for this command."""
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not write updates; only report what would change.",
        )
        parser.add_argument(
            "--only-missing",
            action="store_true",
            help="Only update matches where both stored scores are 0.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Number of MatchData rows per batch.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        """Run the backfill."""
        dry_run = bool(options.get("dry_run"))
        only_missing = bool(options.get("only_missing"))
        batch_size = int(options.get("batch_size") or 500)
        batch_size = max(1, min(batch_size, 5000))

        qs = MatchData.objects.filter(status="finished")
        if only_missing:
            qs = qs.filter(home_score=0, away_score=0)

        ids: list[UUID] = list(qs.values_list("id_uuid", flat=True))
        total = len(ids)
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No finished matches to backfill."))
            return

        self.stdout.write(
            f"Backfilling {total} finished matches (batch_size={batch_size}, "
            f"dry_run={dry_run}, only_missing={only_missing})"
        )

        processed = 0
        updated = 0

        for batch in _chunks(ids, batch_size):
            result = self._process_batch(batch, dry_run=dry_run)
            processed += result.processed
            updated += result.updated

            self.stdout.write(
                f"Processed {processed}/{total} (updated {updated})",
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Processed {processed} finished matches, updated {updated}."
            )
        )

    def _process_batch(self, batch: list[UUID], *, dry_run: bool) -> _BatchResult:
        scores = compute_scores_for_matchdata_ids(batch)

        # Load rows to compare + update.
        rows = list(MatchData.objects.filter(id_uuid__in=batch))

        to_update: list[MatchData] = []
        for row in rows:
            home, away = scores.get(UUID(str(row.id_uuid)), (0, 0))
            if row.home_score == home and row.away_score == away:
                continue
            row.home_score = home
            row.away_score = away
            to_update.append(row)

        if not to_update:
            return _BatchResult(processed=len(rows), updated=0)

        if not dry_run:
            with transaction.atomic():
                MatchData.objects.bulk_update(
                    to_update,
                    ["home_score", "away_score"],
                    batch_size=500,
                )

        return _BatchResult(processed=len(rows), updated=len(to_update))
