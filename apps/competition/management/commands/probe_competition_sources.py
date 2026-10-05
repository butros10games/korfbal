"""Preview stored coverage, or explicitly probe selected public source resources."""

from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.competition.composition import source_probe_client
from apps.competition.models import HistoricalResource
from apps.competition.queries.source_coverage import source_coverage_preview
from apps.competition.services.history import resource_key
from apps.competition.services.source_probe import (
    MAX_PROBE_RESOURCES,
    PROBE_KINDS,
    probe_sources,
    validate_probe,
)
from apps.schedule.domain.competition_context import edition_bounds
from apps.schedule.models import Season


MIN_PROBE_EDITION = 1900
MAX_PROBE_EDITION = 2999


def _validate_configuration(options: dict[str, Any]) -> None:
    """Require an existing edition and explicit budgets for network mode.

    Raises:
        ValueError: The edition or explicit network budget is invalid.

    """
    if not MIN_PROBE_EDITION <= options["edition"] <= MAX_PROBE_EDITION:
        raise ValueError("Provide an edition between 1900 and 2999")
    if not Season.objects.filter(edition=options["edition"]).exists():
        raise ValueError(
            "Select an existing edition; this command never creates seasons"
        )
    if options["probe"] and options["max_requests"] is None:
        raise ValueError("--probe requires an explicit --max-requests budget")


class Command(BaseCommand):
    """Require explicit resource identities and a wire budget before network I/O."""

    help = (
        "Default/--preview: aggregate database coverage, zero HTTP or writes. "
        "--probe: selected public-source reads, no imports/publication/checkpoints; "
        "provider quota/lease accounting and OAuth renewal remain enabled."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Keep network mode distinct from the default read-only preview."""
        parser.add_argument("--edition", type=int, required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--preview", action="store_true")
        mode.add_argument(
            "--probe",
            action="store_true",
            help="Make explicitly budgeted provider requests",
        )
        parser.add_argument(
            "--resource",
            type=int,
            nargs="+",
            help="Existing checkpoint IDs in this edition",
        )
        parser.add_argument("--provider", choices=tuple(PROBE_KINDS))
        parser.add_argument("--kind", choices=sorted(set.union(*PROBE_KINDS.values())))
        parser.add_argument(
            "--source-id",
            nargs="+",
            help="Explicit public provider IDs; no implicit scanning",
        )
        parser.add_argument(
            "--limit", type=int, default=20, help="Maximum selected resources (1-50)"
        )
        parser.add_argument(
            "--max-requests",
            type=int,
            help=(
                "Required with --probe; counts every wire attempt "
                "including authentication"
            ),
        )
        parser.add_argument(
            "--session-file",
            type=Path,
            help="Private app OAuth session file; never printed",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Report only aggregate public evidence and sanitized configuration errors.

        Raises:
            CommandError: The edition, identities or explicit budget are invalid.

        """
        configuration: dict[str, Any] = dict(options)
        try:
            _validate_configuration(configuration)
            if not configuration["probe"]:
                nested = connection.in_atomic_block
                with transaction.atomic():
                    if connection.vendor == "postgresql" and not nested:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SET TRANSACTION ISOLATION LEVEL "
                                "REPEATABLE READ, READ ONLY"
                            )
                    result = {
                        "mode": "preview",
                        **source_coverage_preview(configuration["edition"]),
                    }
            else:
                selected = probe_selection(configuration)
                result = {
                    "mode": "probe",
                    "edition": configuration["edition"],
                    **probe_sources(
                        selected,
                        lambda: source_probe_client(
                            app=any(
                                resource.provider == "app" for resource in selected
                            ),
                            session_file=configuration["session_file"],
                        ),
                        budget=configuration["max_requests"],
                    ),
                }
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise CommandError(
                "Invalid source probe configuration; check existing edition, "
                "public resource "
                "selection, explicit request budget and private session file."
            ) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))


def probe_selection(options: dict[str, Any]) -> list[HistoricalResource]:
    """Build detached resources so fetches cannot change saved checkpoint state.

    Raises:
        ValueError: Identities are missing, unbound, unsupported or exceed the cap.

    """
    limit = options["limit"]
    if not 1 <= limit <= MAX_PROBE_RESOURCES:
        raise ValueError("Select a resource limit from 1 to 50")
    edition = options["edition"]
    if options["resource"]:
        if options["source_id"] or options["provider"] or options["kind"]:
            raise ValueError(
                "Choose existing checkpoint IDs or explicit provider identities"
            )
        keys = set(options["resource"])
        stored = list(
            HistoricalResource.objects
            .filter(
                season__edition=edition,
                pk__in=keys,
            )
            .select_related("season")
            .order_by("pk")
        )
        if len(stored) != len(keys):
            raise ValueError("Every checkpoint must belong to the selected edition")
        selected = [
            HistoricalResource(
                season=row.season,
                provider=row.provider,
                kind=row.kind,
                source_id=row.source_id,
                sport=row.sport,
                start_date=row.start_date,
                end_date=row.end_date,
            )
            for row in stored
        ]
    else:
        if not options["provider"] or not options["kind"] or not options["source_id"]:
            raise ValueError(
                "--probe requires explicit checkpoints or "
                "provider/kind/source-id selection"
            )
        anchors = list(Season.objects.filter(edition=edition, phase="indoor"))
        if len(anchors) != 1:
            raise ValueError(
                "An unambiguous existing indoor edition anchor is required"
            )
        first, last = edition_bounds(edition)
        selected = [
            HistoricalResource(
                season=anchors[0],
                provider=options["provider"],
                kind=options["kind"],
                source_id=identifier,
                start_date=first,
                end_date=last,
            )
            for identifier in dict.fromkeys(options["source_id"])
        ]
    if not selected or len(selected) > limit:
        raise ValueError("The explicit resource selection must fit --limit")
    for resource in selected:
        validate_probe(resource)
        resource.key = resource_key(
            resource.season,
            resource.provider,
            resource.kind,
            resource.source_id,
            (resource.start_date, resource.end_date),
        )
    return selected
