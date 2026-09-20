"""Record that production now serves a previously approved score artifact."""

from hashlib import sha256
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.competition.domain.score_forecast import VERSION
from apps.competition.models import ScoreForecastReview


class Command(BaseCommand):
    """Verify the configured immutable file before closing its decision audit."""

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        """Mark the exact configured artifact active after host health checks.

        Raises:
            CommandError: The configured artifact is missing, invalid or unapproved.
            ValueError: Artifact or review state is inconsistent.

        """
        try:
            path = Path(settings.KORFBAL_SCORE_FORECAST_ARTIFACT)
            raw = path.read_bytes()
            artifact = json.loads(raw)
            digest = sha256(raw).hexdigest()
            if (
                artifact.get("version") != VERSION
                or artifact.get("approved") is not True
                or artifact.get("validation", {}).get("passed") is not True
            ):
                raise ValueError("Configured score forecast is not approved")
            review = ScoreForecastReview.objects.select_for_update().get(
                artifact_sha256=digest
            )
            if review.status != "approved_pending_activation":
                raise ValueError("Forecast review is not approved for activation")
            now = timezone.now()
            review.status = "activated"
            review.decision_history = [
                *review.decision_history,
                {
                    "action": "activated",
                    "status": "activated",
                    "note": "Verified as the configured production artifact",
                    "decided_at": now.isoformat(),
                    "decided_by_id": None,
                },
            ]
            review.save(update_fields=("status", "decision_history", "updated_at"))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ScoreForecastReview.DoesNotExist,
        ) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Recorded active score forecast {digest[:12]}")
