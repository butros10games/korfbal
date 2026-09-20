"""Register aggregate score-forecast evidence for a human admin decision."""

from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.competition.domain.score_forecast import VERSION, timestamp
from apps.competition.models import ScoreForecastReview


def read_json(path: Path) -> tuple[bytes, dict]:
    """Read one private evidence file without logging its contents."""
    raw = path.read_bytes()
    return raw, json.loads(raw)


class Command(BaseCommand):
    """Copy only hashes and aggregate reports into the application database."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require the exact incumbent, candidate and their existing evidence."""
        parser.add_argument("--incumbent", required=True, type=Path)
        parser.add_argument("--candidate", required=True, type=Path)
        parser.add_argument("--validation", required=True, type=Path)
        parser.add_argument("--forward-audit", required=True, type=Path)
        parser.add_argument("--head-to-head", type=Path)
        parser.add_argument("--source-reference", required=True)

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        """Create or refresh an undecided review while preserving human decisions.

        Raises:
            CommandError: Evidence does not belong to the supplied artifacts.
            ValueError: Evidence validation rejects inconsistent provenance.

        """
        values: dict[str, Any] = dict(options)
        try:
            incumbent_raw, incumbent = read_json(values["incumbent"])
            candidate_raw, candidate = read_json(values["candidate"])
            _, validation = read_json(values["validation"])
            _, audit = read_json(values["forward_audit"])
            candidate_hash = sha256(candidate_raw).hexdigest()
            incumbent_hash = sha256(incumbent_raw).hexdigest()
            provenance = (
                incumbent.get("version") == VERSION,
                candidate.get("version") == VERSION,
                candidate.get("approved") is True,
                candidate.get("validation", {}).get("passed") is True,
                validation.get("passed") is True,
                validation == candidate["validation"],
                audit.get("mode") == "deployed-forward-audit",
                audit.get("artifact", {}).get("input_sha256")
                == incumbent.get("input_sha256"),
                audit.get("artifact", {}).get("version") == incumbent.get("version"),
                audit.get("artifact", {}).get("training_cutoff")
                == incumbent.get("training_cutoff"),
                audit.get("artifact", {}).get("available_from")
                == incumbent.get("available_from"),
            )
            if not all(provenance):
                raise ValueError("Forecast review evidence has inconsistent provenance")
            head_to_head = {}
            if values.get("head_to_head"):
                _, head_to_head = read_json(values["head_to_head"])
                hashes = head_to_head.get("artifacts", {})
                if (
                    head_to_head.get("mode") != "artifact-head-to-head"
                    or hashes.get("candidate_sha256") != candidate_hash
                    or hashes.get("incumbent_sha256") != incumbent_hash
                ):
                    raise ValueError("Head-to-head report does not match artifacts")
            defaults = {
                "incumbent_sha256": incumbent_hash,
                "source_reference": values["source_reference"],
                "training_cutoff": timestamp(candidate["training_cutoff"]),
                "available_from": timestamp(candidate["available_from"]),
                "training_matches": candidate["training_matches"],
                "contexts": len(candidate["contexts"]),
                "automated_passed": True,
                "candidate_metrics": validation["metrics"]["candidate"],
                "incumbent_metrics": audit["metrics"]["candidate"],
                "head_to_head": head_to_head,
            }
            review, created = (
                ScoreForecastReview.objects.select_for_update().get_or_create(
                    artifact_sha256=candidate_hash, defaults=defaults
                )
            )
            if not created:
                if review.status not in {"collecting", "rejected"}:
                    raise ValueError("Approved or activated reviews are immutable")
                for field, value in defaults.items():
                    setattr(review, field, value)
                review.save(update_fields=[*defaults, "updated_at"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"{'Registered' if created else 'Updated'} forecast review "
            f"{candidate_hash[:12]} with verdict="
            f"{head_to_head.get('verdict', 'awaiting_head_to_head')}"
        )
