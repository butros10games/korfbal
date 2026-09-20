"""Compare incumbent and candidate score artifacts on one untouched window."""

from argparse import ArgumentParser
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.competition.domain.score_forecast import VERSION, timestamp


class Command(BaseCommand):
    """Write aggregate same-match evidence without changing either artifact."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require frozen artifacts, a later export and an explicit cutoff."""
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument("--incumbent", required=True, type=Path)
        parser.add_argument("--candidate", required=True, type=Path)
        parser.add_argument("--report", required=True, type=Path)
        parser.add_argument("--through", required=True, type=timestamp)

    def handle(self, *args: object, **options: object) -> None:
        """Validate provenance and save the shared forward comparison.

        Raises:
            CommandError: Inputs, artifacts or the requested window are invalid.
            ValueError: Artifact comparison rejects inconsistent provenance.

        """
        values: dict[str, Any] = dict(options)
        compare = import_module(
            "apps.competition.offline.score_validation"
        ).head_to_head
        try:
            exported = json.loads(values["input"].read_bytes())
            incumbent_raw = values["incumbent"].read_bytes()
            candidate_raw = values["candidate"].read_bytes()
            incumbent = json.loads(incumbent_raw)
            candidate = json.loads(candidate_raw)
            if exported["schema"] != 1 or not exported["rows"]:
                raise ValueError("Unsupported or empty forecast export")
            if any(
                artifact.get("version") != VERSION
                for artifact in (incumbent, candidate)
            ):
                raise ValueError("Unsupported score forecast artifact")
            through = values["through"]
            if through > timestamp(exported["exported_at"]):
                raise ValueError("Comparison cutoff cannot exceed export time")
            report = compare(exported["rows"], incumbent, candidate, through)
            report["artifacts"] = {
                "incumbent_sha256": sha256(incumbent_raw).hexdigest(),
                "candidate_sha256": sha256(candidate_raw).hexdigest(),
            }
            values["report"].write_text(
                json.dumps(report, indent=2, allow_nan=False) + "\n"
            )
            values["report"].chmod(0o600)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Compared {report['evaluated_matches']} matches across "
            f"{report['evaluated_pools']} poules; verdict={report['verdict']}"
        )
