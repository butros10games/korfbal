"""Model for a season."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

from bg_uuidv7 import uuidv7
from django.db import models

from apps.schedule.domain.competition_context import (
    DISCIPLINES,
    PHASES,
    SOURCES,
    SeasonContext,
    resolve_context,
)


class Season(models.Model):
    """Model for a season."""

    objects: ClassVar[models.Manager[Season]]

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )
    name: models.CharField[str, str] = models.CharField(max_length=255, unique=True)
    start_date: models.DateField[date, date] = models.DateField()
    end_date: models.DateField[date, date] = models.DateField()
    # Explicit competition context. Blank or null values are unresolved; the
    # display name is never read to fill them in.
    edition = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Start year of the July-June korfbal year (2025 for 2025-2026).",
    )
    # Database defaults keep rows written by older code (rolling deploys) valid.
    discipline = models.CharField(
        max_length=10,
        blank=True,
        default="",
        db_default="",
        choices=[(value, value) for value in DISCIPLINES],
    )
    phase = models.CharField(
        max_length=12,
        blank=True,
        default="",
        db_default="",
        choices=[(value, value) for value in PHASES],
        help_text="autumn/spring halves, full_season for continuous outdoor play.",
    )
    context_source = models.CharField(
        max_length=20,
        blank=True,
        default="",
        db_default="",
        choices=[(value, value) for value in SOURCES],
    )
    data_unavailable = models.BooleanField(
        default=False,
        db_default=False,
        help_text=(
            "Legacy no-data marker for older clients; available fixtures remain "
            "browsable even when discovery coverage is incomplete."
        ),
    )
    data_coverage = models.CharField(
        max_length=12,
        default="unknown",
        db_default="unknown",
        choices=[
            (value, value)
            for value in ("unknown", "partial", "complete", "unavailable")
        ],
        help_text="Source-result coverage; complete requires reviewed schedule proof.",
    )
    coverage_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_default="",
        help_text="Public explanation of the coverage evidence and its scope.",
    )

    class Meta:
        """Require a valid inclusive season interval."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="season_valid_date_interval",
            ),
            models.CheckConstraint(
                condition=models.Q(edition__isnull=True)
                | models.Q(edition__gte=1900, edition__lte=2999),
                name="season_plausible_edition",
            ),
        ]

    def __str__(self) -> str:
        """Get the string representation of the season.

        Returns:
            str: The name of the season.

        """
        return str(self.name)

    @property
    def context(self) -> SeasonContext:
        """Return the stored competition context, with explicit unknowns."""
        return resolve_context(self)
