"""Train and backtest versioned score artifacts offline."""

from argparse import ArgumentParser
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.competition.domain.score_forecast import (
    CONTEXT_FIELDS,
    MAX_DURATION,
    timestamp,
)


class Command(BaseCommand):
    """Keep numerical fitting offline and approval dependent on held-out evidence."""

    help = (
        "Fit a hierarchical score model; use repeated --origin for rolling validation."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require explicit chronological splits and output paths."""
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument("--output", required=True, type=Path)
        parser.add_argument("--report", required=True, type=Path)
        parser.add_argument("--origin", required=True, action="append", type=timestamp)
        parser.add_argument("--cutoff", required=True, type=timestamp)
        parser.add_argument("--approve", action="store_true")
        parser.add_argument("--cold-start", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Validate exports and require promotion evidence.

        Raises:
            CommandError: Export is invalid or the promotion gate fails.
            ValueError: An export field is invalid (translated to CommandError).

        """
        values: dict[str, Any] = dict(options)
        fit = import_module("apps.competition.offline.score_training").fit
        backtest = import_module("apps.competition.offline.score_validation").backtest

        try:
            raw = values["input"].read_bytes()
            data = json.loads(raw)
            rows = data["rows"]
            cutoff = values["cutoff"]
            origins = sorted(set(values["origin"]))
            if (
                not origins
                or origins[-1] >= cutoff
                or cutoff > timestamp(data["exported_at"])
            ):
                raise ValueError(
                    "Origins must precede cutoff; cutoff must not exceed export time"
                )
            if data["schema"] != 1 or not rows:
                raise ValueError("Unsupported or empty forecast export")
            if len({row["match"] for row in rows}) != len(rows):
                raise ValueError("Duplicate match identities")
            for row in rows:
                if any(
                    not isinstance(row.get(key), str) or "|" in row[key]
                    for key in CONTEXT_FIELDS
                ):
                    raise ValueError("Missing or invalid competition context")
                if (
                    not 0 < row["duration"] <= MAX_DURATION
                    or row["home"] == row["away"]
                ):
                    raise ValueError("Invalid duration or team identity")
            validation = backtest(
                rows, origins, cutoff, cold_start=values["cold_start"]
            )
            artifact = fit(rows, cutoff)
            artifact.update({
                "input_sha256": sha256(raw).hexdigest(),
                "available_from": timezone.now().isoformat(),
                "metadata_history": data["metadata_history"],
                "validation": validation,
                "approved": bool(values["approve"] and validation["passed"]),
            })
            values["report"].write_text(
                json.dumps(validation, indent=2, allow_nan=False) + "\n"
            )
            values["output"].write_text(
                json.dumps(artifact, separators=(",", ":"), allow_nan=False) + "\n"
            )
            if values["approve"] and not artifact["approved"]:
                raise CommandError(
                    "Promotion gate failed; candidate artifact and diagnostic "
                    "report saved, not approved"
                )
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Saved {len(artifact['contexts'])} contexts; "
            f"approved={artifact['approved']}; "
            "configure KORFBAL_SCORE_FORECAST_ARTIFACT only after approval"
        )
