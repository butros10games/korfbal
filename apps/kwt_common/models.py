"""Durable background work, independent of broker availability."""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class BackgroundJob(models.Model):
    """One coalescing work item or one immutable notification intent."""

    key = models.CharField(max_length=255, unique=True)
    task = models.CharField(max_length=160)
    args = models.JSONField(default=list)
    kwargs = models.JSONField(default=dict)
    queue = models.CharField(max_length=40, default="celery")
    generation = models.PositiveBigIntegerField(default=1)
    completed_generation = models.PositiveBigIntegerField(default=0)
    due_at = models.DateTimeField(default=timezone.now, null=True)
    published_until = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=160, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Bound due scans independently for each worker pool."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["queue", "due_at"], name="background_queue_due")
        ]

    def __str__(self) -> str:
        """Identify work without exposing notification payloads."""
        return self.key
