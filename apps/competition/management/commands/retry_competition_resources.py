"""Preview or requeue explicitly scoped failures after fixing their cause."""

from argparse import ArgumentParser
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.competition.models import SyncLease, SyncResource
from apps.competition.services.resources import ENDPOINTS
from apps.schedule.models import Season


class Command(BaseCommand):
    """Preserve successful checkpoints and the provider-wide traffic budget."""

    help = "Preview failed feeds by season/kind; use --apply after fixing the cause."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require a narrow scope and make mutation explicit."""
        parser.add_argument("--season", required=True)
        parser.add_argument("--kind", required=True, choices=sorted(ENDPOINTS))
        parser.add_argument("--error-code")
        parser.add_argument("--resource-id", type=int, action="append")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Preview safely or reset failures while holding the global lease lock.

        Raises:
            CommandError: The season is unknown or a worker/cooldown is active.

        """
        try:
            season = Season.objects.get(name=options["season"])
        except Season.DoesNotExist as exc:
            raise CommandError("Provide an existing season") from exc
        resources = SyncResource.objects.filter(
            season=season,
            kind=options["kind"],
            failures__gt=0,
        )
        if options["error_code"]:
            resources = resources.filter(last_error=options["error_code"])
        if options["resource_id"]:
            resources = resources.filter(pk__in=options["resource_id"])
        if not options["apply"]:
            self.stdout.write(
                json.dumps({"dry_run": True, "matched": resources.count()})
            )
            return
        now = timezone.now()
        with transaction.atomic():
            lease, _ = SyncLease.objects.select_for_update().get_or_create(
                key="sportlink",
                defaults={"expires_at": now},
            )
            if lease.expires_at > now:
                raise CommandError(
                    "An importer or provider cooldown is active; "
                    "retry after the lease expires"
                )
            # Keep last_error for diagnosis until success; force a full response so
            # a parser fix also repairs rows with an earlier successful checkpoint.
            reset = resources.update(failures=0, next_sync_at=now, etag="")
        self.stdout.write(json.dumps({"dry_run": False, "requeued": reset}))
