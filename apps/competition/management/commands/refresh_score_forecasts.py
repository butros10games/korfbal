"""Run an immutable forward audit and gated refit in a bounded batch worker."""

from argparse import ArgumentParser
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.competition.domain.score_forecast import timestamp


def saved_export(options: dict[str, object]) -> tuple[bytes | None, datetime]:
    """Keep a reused export's original observation boundary.

    Raises:
        CommandError: Export season or cutoff is invalid.

    """
    raw = Path(str(options["input"])).read_bytes() if options.get("input") else None
    saved = json.loads(raw) if raw is not None else None
    through = options.get("through") or (
        timestamp(saved["exported_at"]) if saved else timezone.now()
    )
    if not isinstance(through, datetime):
        raise CommandError("Expected a timezone-aware cutoff")
    if saved is not None and (
        saved["schema"] != 1
        or through > timestamp(saved["exported_at"])
        or any(row["season"] != str(options["season"]) for row in saved["rows"])
    ):
        raise CommandError("Saved export does not match season or cutoff")
    return raw, through


def compare_challenger(
    options: dict[str, object],
    root: Path,
    through: datetime,
    record: Callable[[str], None],
    stdout: object,
) -> None:
    """Write optional same-window evidence before fitting another candidate."""
    challenger = options.get("challenger")
    if not challenger:
        return
    record("comparing_challenger")
    call_command(
        "compare_score_forecasts",
        input=root / "input.json",
        incumbent=root / "served-artifact.json",
        candidate=challenger,
        report=root / "head-to-head.json",
        through=through,
        stdout=stdout,
    )


class Command(BaseCommand):
    """Store evidence before fitting; never activate the candidate automatically."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an explicit season, served artifact and new run directory."""
        parser.add_argument("--season", required=True, type=UUID)
        parser.add_argument("--artifact", required=True, type=Path)
        parser.add_argument("--output-dir", required=True, type=Path)
        parser.add_argument("--through", type=timestamp)
        parser.add_argument(
            "--challenger",
            type=Path,
            help="Previously fitted candidate to compare on this untouched round",
        )
        parser.add_argument(
            "--input", type=Path, help="Reuse a saved export after a failed worker run"
        )

    def handle(self, *args: object, **options: object) -> None:
        """Preserve provenance and record failed gates as a completed decision.

        Raises:
            CommandError: Inputs are invalid, a run exists or execution fails.

        """
        root = Path(str(options["output_dir"]))
        try:
            raw = Path(str(options["artifact"])).read_bytes()
            artifact = json.loads(raw)
            cutoff = timestamp(artifact["training_cutoff"])
            saved_input, through = saved_export(options)
            origins = sorted({
                timestamp(fold["cutoff"])
                for fold in artifact["validation"]["folds"]
                if timestamp(fold["cutoff"]) < cutoff
            })
            if (
                not artifact["approved"]
                or not artifact["validation"]["passed"]
                or not origins
            ):
                raise CommandError(
                    "Require an approved artifact with chronological validation"
                )
            if (
                not cutoff
                <= timestamp(artifact["available_from"])
                < through
                <= timezone.now()
            ):
                raise CommandError("Invalid forward audit cutoff")
            if any(
                key.split("|")[0] != str(options["season"])
                for key in artifact["contexts"]
            ):
                raise CommandError("Season does not match deployed artifact")
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CommandError(str(error)) from error
        (root / "served-artifact.json").write_bytes(raw)
        state = {
            "status": "exporting",
            "through": through.isoformat(),
            "served_sha256": sha256(raw).hexdigest(),
        }

        def record(status: str) -> None:
            state["status"] = status
            temporary = root / "status.tmp"
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            temporary.replace(root / "status.json")
            self.stdout.write(status)

        record("exporting")
        try:
            if saved_input is not None:
                (root / "input.json").write_bytes(saved_input)
                record("using_saved_export")
            else:
                call_command(
                    "export_score_forecasts",
                    season=options["season"],
                    output=root / "input.json",
                    legacy_from=origins[-1],
                    through=through,
                    stdout=self.stdout,
                )
            state["input_sha256"] = sha256(
                (root / "input.json").read_bytes()
            ).hexdigest()
            record("auditing")
            call_command(
                "audit_score_forecasts",
                input=root / "input.json",
                artifact=root / "served-artifact.json",
                report=root / "forward-audit.json",
                through=through,
                stdout=self.stdout,
            )
            compare_challenger(options, root, through, record, self.stdout)
            record("fitting")
            try:
                call_command(
                    "fit_score_forecasts",
                    "--origin",
                    origins[-1].isoformat(),
                    "--origin",
                    cutoff.isoformat(),
                    input=root / "input.json",
                    output=root / "candidate.json",
                    report=root / "validation.json",
                    cutoff=through,
                    approve=True,
                    stdout=self.stdout,
                )
            except CommandError:
                candidate = root / "candidate.json"
                if (
                    not candidate.exists()
                    or json.loads(candidate.read_text())["approved"]
                ):
                    raise
                record("rejected")
                return
            record("passed_gates_pending_review")
        except Exception:
            record("failed")
            raise
