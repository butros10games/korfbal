"""Schedule lossless repackaging of recordings and retire superseded objects."""

from datetime import timedelta

from django.utils import timezone

from apps.kwt_common.services.jobs import enqueue


REPACKAGE = "apps.video_analysis.tasks.repackage_recording"
DELETE = "apps.video_analysis.tasks.delete_superseded_video"
# Clip and frame jobs that already stream the old object may keep reading it.
SUPERSEDED_GRACE = timedelta(days=1)


def schedule(workspace_id: object, relative: str) -> None:
    """Persist repackaging intent; the vision worker downloads and regroups."""
    enqueue(
        REPACKAGE,
        f"{workspace_id}:{relative}",
        args=[str(workspace_id), relative],
        queue="vision",
    )


def retire(workspace_id: object, key: str) -> None:
    """Delete a superseded object after running readers have finished."""
    enqueue(
        DELETE,
        f"{workspace_id}:{key}",
        args=[str(workspace_id), key],
        queue="vision",
        due_at=timezone.now() + SUPERSEDED_GRACE,
        once=True,
    )
