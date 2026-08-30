"""Tests for audit management commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
import pytest

from apps.audit.management.commands import purge_audit_events
from apps.audit.models import AuditEvent


TOTAL_AUDIT_ROWS = 3
REMAINING_AFTER_PURGE = 1


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_purge_audit_events_dry_run_keeps_rows() -> None:
    """Dry-run should report deletions without mutating data."""
    now = timezone.now()
    AuditEvent.objects.create(
        event_name="old.one",
        source_system="test",
        occurred_at=now - timedelta(days=100),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="old.two",
        source_system="test",
        occurred_at=now - timedelta(days=95),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="new.one",
        source_system="test",
        occurred_at=now - timedelta(days=1),
        severity="info",
    )

    out = StringIO()
    call_command("purge_audit_events", days=90, dry_run=True, stdout=out)

    assert "Would delete 2" in out.getvalue()
    assert AuditEvent.objects.count() == TOTAL_AUDIT_ROWS


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_purge_audit_events_deletes_old_rows() -> None:
    """Non-dry-run should delete rows older than the retention threshold."""
    now = timezone.now()
    AuditEvent.objects.create(
        event_name="old.one",
        source_system="test",
        occurred_at=now - timedelta(days=101),
        severity="info",
    )
    AuditEvent.objects.create(
        event_name="old.two",
        source_system="test",
        occurred_at=now - timedelta(days=91),
        severity="warning",
    )
    recent = AuditEvent.objects.create(
        event_name="new.one",
        source_system="test",
        occurred_at=now - timedelta(days=2),
        severity="info",
    )

    out = StringIO()
    call_command("purge_audit_events", days=90, dry_run=False, stdout=out)

    assert "Deleted 2" in out.getvalue()
    assert AuditEvent.objects.count() == REMAINING_AFTER_PURGE
    assert AuditEvent.objects.filter(id_uuid=recent.id_uuid).exists()


@pytest.mark.django_db
@override_settings(KORFBAL_AUDIT_RETENTION_DAYS=7, SECURE_SSL_REDIRECT=False)
def test_purge_audit_events_uses_configured_retention_and_keeps_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default command cutoff should come from settings and remain exclusive."""
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    cutoff = now - timedelta(days=7)
    monkeypatch.setattr(purge_audit_events.timezone, "now", lambda: now)
    expired = AuditEvent.objects.create(
        event_name="expired",
        source_system="test",
        occurred_at=cutoff - timedelta(microseconds=1),
    )
    boundary = AuditEvent.objects.create(
        event_name="boundary",
        source_system="test",
        occurred_at=cutoff,
    )

    call_command("purge_audit_events", stdout=StringIO())

    assert not AuditEvent.objects.filter(id_uuid=expired.id_uuid).exists()
    assert AuditEvent.objects.filter(id_uuid=boundary.id_uuid).exists()
