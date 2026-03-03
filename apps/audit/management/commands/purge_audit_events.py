"""Purge old audit events to keep timeline tables bounded."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from apps.audit.models import AuditEvent


DEFAULT_RETENTION_DAYS = 90


class Command(BaseCommand):
    """Delete audit rows older than a configurable retention window."""

    help = "Purge old audit events based on occurred_at timestamp."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register command arguments."""
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Retention in days (default: KORFBAL_AUDIT_RETENTION_DAYS or 90).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many rows would be deleted without deleting.",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute purge command."""
        days_opt = options.get("days")
        days = self._resolve_days(days_opt)
        dry_run = bool(options.get("dry_run"))

        cutoff = timezone.now() - timedelta(days=days)
        queryset = AuditEvent._default_manager.filter(occurred_at__lt=cutoff)
        total = queryset.count()

        if dry_run:
            self.stdout.write(
                f"[dry-run] Would delete {total} audit events older than "
                f"{cutoff.isoformat()}."
            )
            return

        deleted, _details = queryset.delete()
        self.stdout.write(
            f"Deleted {deleted} audit events older than {cutoff.isoformat()}."
        )

    def _resolve_days(self, value: object) -> int:
        if isinstance(value, int):
            return max(1, value)

        configured = getattr(
            settings,
            "KORFBAL_AUDIT_RETENTION_DAYS",
            DEFAULT_RETENTION_DAYS,
        )
        if isinstance(configured, int):
            return max(1, configured)

        return DEFAULT_RETENTION_DAYS
