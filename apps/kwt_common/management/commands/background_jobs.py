"""Inspect durable work and explicitly retry an exhausted job."""

import json

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone

from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.tasks import publish_job


class Command(BaseCommand):
    """Expose safe operational state without notification payloads or credentials."""

    help = "Show pending/failed work by queue; --retry-id retries one exhausted job."

    def add_arguments(self, parser: CommandParser) -> None:
        """Allow an explicit retry, never implicitly replay completed notifications."""
        parser.add_argument("--retry-id", type=int)

    def handle(self, *args: object, **options: object) -> None:
        """Print counts and retry an unfinished failed job when requested.

        Raises:
            CommandError: The requested job is not an exhausted failure.

        """
        pending = BackgroundJob.objects.filter(completed_generation__lt=F("generation"))
        if (job_id := options.get("retry_id")) and not pending.filter(
            pk=job_id, due_at=None
        ).update(due_at=timezone.now(), attempts=0, error=""):
            raise CommandError("No exhausted unfinished job with that ID")
        if job_id:
            transaction.on_commit(lambda: publish_job(int(str(job_id))), robust=True)
        self.stdout.write(
            json.dumps({
                "counts": list(
                    pending.values("queue", "error").annotate(count=Count("pk"))
                ),
                "failed": list(
                    pending.filter(due_at=None).values("pk", "task", "queue", "error")[
                        :100
                    ]
                ),
            })
        )
