"""Compatibility accessors for ordered relational song selections."""

from collections.abc import Iterable
from typing import Any, ClassVar

from django.db import models, transaction
from django.db.models.base import ModelBase


class OrderedSongSelectionModel(models.Model):
    """Preserve the list API while storing selections as foreign-key relations."""

    goal_song_selections: models.Manager[Any]
    selection_field: ClassVar[str]
    selection_owner: ClassVar[str]

    class Meta:
        """Do not create a table for this shared persistence behavior."""

        abstract = True

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Commit the owner and its selection atomically, preserving list order."""
        fields = set(update_fields) if update_fields is not None else None
        replace = "_pending_song_selection" in self.__dict__ and (
            fields is None or self.selection_field in fields
        )
        if fields is not None:
            fields.discard(self.selection_field)
        with transaction.atomic(using=using):
            super().save(
                force_insert=force_insert,
                force_update=force_update,
                using=using,
                update_fields=fields,
            )
            if replace:
                type(self)._base_manager.using(using).select_for_update().get(
                    pk=self.pk
                )
                manager = self.goal_song_selections
                ids = self.__dict__["_pending_song_selection"]
                manager.all().delete()
                manager.bulk_create([
                    manager.model(**{
                        self.selection_owner: self,
                        "song_id": song_id,
                        "position": position,
                    })
                    for position, song_id in enumerate(ids)
                ])
                self.__dict__.pop("_pending_song_selection")
                self.__dict__.get("_prefetched_objects_cache", {}).pop(
                    "goal_song_selections", None
                )

    def selected_song_ids(self) -> list[str]:
        """Return the pending selection or the ordered persisted references."""
        if "_pending_song_selection" in self.__dict__:
            return list(self.__dict__["_pending_song_selection"])
        return [str(row.song_id) for row in self.goal_song_selections.all()]

    def set_selected_song_ids(self, values: list[str]) -> None:
        """Stage selection changes until the containing model is saved."""
        self.__dict__["_pending_song_selection"] = list(dict.fromkeys(values))
