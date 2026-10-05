"""Preview or apply bounded standings repairs from a reviewed manifest."""

from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.standings_repair import (
    STAGES,
    Selection,
    apply,
    preview,
)


class Command(BaseCommand):
    """Read-only by default; --apply only acts on a preview's manifest."""

    help = (
        "Report (or with --apply and a preview manifest repair) closed poules "
        "with empty official tables, and convert or clear legacy generated "
        "standings. Never contacts a provider."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit scope and a manifest for every write."""
        parser.add_argument("--stage", choices=STAGES, default="blank-closed")
        parser.add_argument("--edition", type=int, action="append", default=[])
        parser.add_argument("--pool", type=int, action="append", default=[])
        parser.add_argument("--after", type=int, default=0)
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument(
            "--manifest",
            type=Path,
            help="Preview: write the manifest here. Apply: the reviewed manifest",
        )
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Write the JSON report; ``next_after`` resumes a partial preview.

        Raises:
            CommandError: The selection or manifest is invalid.

        """
        values: dict[str, Any] = dict(options)
        manifest: Path | None = values["manifest"]
        try:
            if values["apply"]:
                if manifest is None:
                    raise CommandError("--apply requires the preview's --manifest")
                plan = json.loads(manifest.read_text(encoding="utf-8"))
                if plan.get("stage") != values["stage"]:
                    raise CommandError("The manifest belongs to another --stage")
                report = apply(plan)
            else:
                report = preview(
                    Selection(
                        stage=values["stage"],
                        editions=tuple(values["edition"]),
                        pool_ids=tuple(values["pool"]),
                        after=values["after"],
                        limit=values["limit"],
                    )
                )
                if manifest is not None:
                    manifest.write_text(
                        json.dumps(report.as_payload(), indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report.as_payload(), indent=2, sort_keys=True))
