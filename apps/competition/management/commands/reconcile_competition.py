"""Preview or apply links between the catalogue and existing application records."""

from argparse import ArgumentParser
import json
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from apps.competition.services.reconciliation import reconcile


def read_overrides(path: Path) -> dict[tuple[str, int], str]:
    """Read operator-selected identities with strict source and UUID validation.

    Raises:
        TypeError: The mapping file is not an array.
        ValueError: A UUID is invalid or a source appears more than once.

    """
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise TypeError("Mappings must be a JSON array")
    overrides = {}
    for row in rows:
        key = (str(row["kind"]), int(row["source_id"]))
        if key in overrides:
            raise ValueError("Duplicate source mapping")
        overrides[key] = str(UUID(str(row["local_id"])))
    return overrides


class Command(BaseCommand):
    """Reconcile all imported seasons without modifying local match or roster data."""

    help = "Preview existing-record links; --apply saves unique and explicit mappings."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept optional operator-reviewed mappings and explicit apply mode."""
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--links-file",
            type=Path,
            help="JSON array of {kind, source_id, local_id} mappings",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Print all decisions and unmatched local records as JSON.

        Raises:
            CommandError: Mappings are malformed, stale or inconsistent.

        """
        try:
            path = options.get("links_file")
            overrides = read_overrides(path) if isinstance(path, Path) else {}
            result = reconcile(apply=bool(options["apply"]), overrides=overrides)
        except (ValueError, TypeError, KeyError, OSError, IntegrityError) as exc:
            raise CommandError(f"Reconciliation was not applied: {exc}") from exc
        self.stdout.write(json.dumps(result, sort_keys=True, indent=2))
