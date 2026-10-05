"""Preview source/native fixture linkage and apply an inspected bounded manifest."""

from argparse import ArgumentParser
import json
from pathlib import Path
from typing import cast

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.linkage_review import (
    MAX_LINKAGE_SELECTION,
    LinkageSelection,
    apply_manifest,
    preview_linkage,
)
from apps.schedule.models import Season


class Command(BaseCommand):
    """Review exact fixture identity and protect native sporting history."""

    help = (
        "Review fixture mismatches and selection reachability; "
        "apply only proven manifest entries."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require a bounded scope for previews and an exact manifest for writes."""
        parser.add_argument("--season")
        parser.add_argument("--edition", type=int)
        parser.add_argument("--pool-id", action="append")
        parser.add_argument("--source-id", action="append")
        parser.add_argument("--reason", action="append")
        parser.add_argument("--cursor", type=int, default=0)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--manifest", type=Path)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Preview read-only; a changed fingerprint blocks the corresponding apply.

        Raises:
            CommandError: Scope or manifest is invalid.

        """
        try:
            manifest_path = cast(Path | None, options["manifest"])
            if options["apply"]:
                if manifest_path is None:
                    raise CommandError(
                        "--apply requires a previously reviewed --manifest file"
                    )
                result = {
                    "dry_run": False,
                    "http_requests": 0,
                    "counts": apply_manifest(json.loads(manifest_path.read_text())),
                }
            else:
                if not any(
                    options.get(key)
                    for key in ("season", "edition", "pool_id", "source_id")
                ):
                    raise CommandError(
                        "Provide --season, --edition, --pool-id or exact --source-id"
                    )
                limit = int(str(options["limit"]))
                cursor = int(str(options["cursor"]))
                if not 1 <= limit <= MAX_LINKAGE_SELECTION or cursor < 0:
                    raise CommandError("Limit must be 1..1000 and cursor nonnegative")
                season = (
                    Season.objects.get(name=str(options["season"]))
                    if options.get("season")
                    else None
                )
                selection = LinkageSelection(
                    season_id=season.pk if season else None,
                    edition=cast(int | None, options.get("edition")),
                    pool_ids=tuple(cast(list[str], options.get("pool_id") or [])),
                    source_ids=tuple(cast(list[str], options.get("source_id") or [])),
                    reasons=tuple(cast(list[str], options.get("reason") or [])),
                    after=cursor,
                    limit=limit,
                )
                result = preview_linkage(selection)
                if manifest_path:
                    manifest_path.write_text(
                        json.dumps(result, sort_keys=True, indent=2) + "\n"
                    )
        except (Season.DoesNotExist, OSError, ValueError, KeyError, TypeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
