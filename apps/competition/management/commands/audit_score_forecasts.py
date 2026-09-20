"""Audit one approved score artifact over its untouched forward window."""

from argparse import ArgumentParser
from importlib import import_module
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.competition.domain.score_forecast import VERSION, timestamp


class Command(BaseCommand):
    """Record aggregate metrics for the exact artifact that production served."""

    help = "Audit an approved immutable score artifact after it became available."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require explicit reproducible inputs and observation cutoff."""
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument("--artifact", required=True, type=Path)
        parser.add_argument("--report", required=True, type=Path)
        parser.add_argument("--through", required=True, type=timestamp)

    def handle(self, *args: object, **options: object) -> None:
        """Audit without fitting or mutating source data.

        Raises:
            CommandError: Inputs or the requested audit window are invalid.
            ValueError: The loaded export or artifact cannot be decoded.

        """
        values: dict[str, Any] = dict(options)
        forward_audit = import_module(
            "apps.competition.offline.score_validation"
        ).forward_audit
        try:
            data = json.loads(values["input"].read_bytes())
            artifact = json.loads(values["artifact"].read_bytes())
            if data["schema"] != 1 or not data["rows"]:
                raise ValueError("Unsupported or empty forecast export")
            if artifact.get("version") != VERSION:
                raise ValueError("Unsupported score forecast artifact")
            through = values["through"]
            if through > timestamp(data["exported_at"]):
                raise ValueError("Audit cutoff cannot exceed export time")
            audit = forward_audit(data["rows"], artifact, through)
            values["report"].write_text(
                json.dumps(audit, indent=2, allow_nan=False) + "\n"
            )
            values["report"].chmod(0o600)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Audited {audit['evaluated_matches']} matches across "
            f"{audit['evaluated_pools']} poules"
        )
