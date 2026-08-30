"""Base model for typed projections materialized from canonical events."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from importlib import import_module
from typing import Protocol, cast, override

from django.db import models, transaction
from django.db.models.base import ModelBase

from apps.game_tracker.services.match_event_context import (
    match_data_is_deleting,
    match_event_recording_is_suppressed,
    suppress_match_event_recording,
)


class _EventRecorder(Protocol):
    def __call__(self, instance: object, *, operation: str) -> object: ...


def _record_event(instance: object, *, operation: str) -> None:
    """Load the recorder lazily to avoid a models-package import cycle."""
    module = import_module("apps.game_tracker.services.match_events")
    recorder = cast(_EventRecorder, module.record_typed_match_event)
    recorder(instance, operation=operation)


class EventProjectionQuerySet(models.QuerySet[models.Model]):
    """Keep bulk ORM mutations on the same canonical event-first path."""

    def delete(self) -> tuple[int, dict[str, int]]:
        """Retract every selected fact before deleting its projection row."""
        if match_event_recording_is_suppressed():
            return super().delete()
        deleted = 0
        model_counts: dict[str, int] = {}
        with transaction.atomic():
            for instance in self.iterator():
                count, counts = instance.delete()
                deleted += count
                for model_name, model_count in counts.items():
                    model_counts[model_name] = (
                        model_counts.get(model_name, 0) + model_count
                    )
        return deleted, model_counts

    def update(self, **kwargs: object) -> int:
        """Append one new version for every selected projection update."""
        if match_event_recording_is_suppressed():
            return super().update(**kwargs)
        updated = 0
        with transaction.atomic():
            for instance in self.iterator():
                for field, value in kwargs.items():
                    setattr(instance, field, value)
                instance.save(update_fields=kwargs)
                updated += 1
        return updated

    @override
    def bulk_create(
        self,
        objs: Iterable[models.Model],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[models.Model]:
        """Append each fact before materializing a requested bulk insert.

        Raises:
            ValueError: When conflict handling would make event identity ambiguous.

        """
        instances = list(objs)
        if match_event_recording_is_suppressed():
            return list(
                super().bulk_create(
                    instances,
                    batch_size=batch_size,
                    ignore_conflicts=ignore_conflicts,
                    update_conflicts=update_conflicts,
                    update_fields=update_fields,
                    unique_fields=unique_fields,
                )
            )
        if ignore_conflicts or update_conflicts:
            msg = "Conflict-handling bulk writes cannot preserve event identity"
            raise ValueError(msg)
        with transaction.atomic():
            for instance in instances:
                instance.save(force_insert=True)
        return instances

    def bulk_update(
        self,
        objs: Iterable[models.Model],
        fields: Iterable[str],
        batch_size: int | None = None,
    ) -> int:
        """Append each replacement before materializing bulk updates.

        Raises:
            ValueError: When no fields are specified.

        """
        instances = list(objs)
        field_names = list(fields)
        if not field_names:
            msg = "Field names must be given to bulk_update()."
            raise ValueError(msg)
        if match_event_recording_is_suppressed():
            return super().bulk_update(
                instances,
                field_names,
                batch_size=batch_size,
            )
        with transaction.atomic():
            for instance in instances:
                instance.save(update_fields=field_names)
        return len(instances)


class EventProjectionManager(models.Manager[models.Model]):
    """Default manager exposing canonical mutation-aware querysets."""

    def get_queryset(self) -> EventProjectionQuerySet:
        """Return the canonical mutation-aware queryset."""
        return EventProjectionQuerySet(self.model, using=self._db)


class EventProjectionModel(models.Model):
    """Append the canonical fact before changing its relational projection."""

    objects = EventProjectionManager()

    class Meta:
        """Keep this orchestration model out of the database schema."""

        abstract = True

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Append a canonical version before saving its query projection."""
        if match_event_recording_is_suppressed():
            super().save(
                force_insert=force_insert,
                force_update=force_update,
                using=using,
                update_fields=update_fields,
            )
            return

        operation = "created" if self._state.adding else "updated"
        with transaction.atomic():
            _record_event(self, operation=operation)
            with suppress_match_event_recording():
                super().save(
                    force_insert=force_insert,
                    force_update=force_update,
                    using=using,
                    update_fields=update_fields,
                )

    def delete(
        self,
        using: str | None = None,
        keep_parents: bool = False,
    ) -> tuple[int, dict[str, int]]:
        """Append a retraction before removing its query projection."""
        if match_event_recording_is_suppressed():
            return super().delete(using=using, keep_parents=keep_parents)

        match_data_id = self.__dict__.get("match_data_id")
        with transaction.atomic():
            if match_data_id is None or not match_data_is_deleting(match_data_id):
                _record_event(self, operation="deleted")
            with suppress_match_event_recording():
                return super().delete(using=using, keep_parents=keep_parents)
