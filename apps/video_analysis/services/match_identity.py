"""Keep a whole recording's match-wide names current after reviewer answers.

A finished replay with a roster is named once by its match pass
(``engine/clip_match_wide.py``). A reviewer's answer in any section changes
what that pass would publish: the section's own cache is updated at once, but
the replay's published links (what playback shows without the naming panel,
and every other section's names) only change when the pass runs again. An
applied answer therefore queues one debounced pass on the vision worker
(``identity_review.schedule_match_pass``); a burst of answers coalesces into a
single run, and a pass whose inputs did not change since the last one is a
no-op. Until it has run, ``stale`` tells
readers that the published names predate an answer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from apps.video_analysis.engine.clips import directory
from apps.video_analysis.engine.store import atomic_json
from apps.video_analysis.models import AnalysisJob, ClipIdentityReview
from apps.video_analysis.services.identity_review import hydrate, named_replay


if TYPE_CHECKING:
    from apps.video_analysis.application.ports import MatchIdentityRuntime


def current_reviews(job_id: str) -> dict[str, int]:
    """Return the applied answer revision of each reviewed section."""
    return dict(
        ClipIdentityReview.objects
        .filter(job_id=job_id)
        .exclude(section="")
        .values_list("section", "revision")
    )


def stale(job_id: str, wide: dict[str, Any] | None) -> bool:
    """Whether a reviewer answered since the replay's match pass solved.

    Returns:
        True when any section the pass used holds newer answers.

    """
    if not wide or wide.get("status") != "completed":
        return False
    used = wide.get("section_reviews") or {}
    reviews = current_reviews(job_id)
    return any(
        reviews.get(section, 0) != revision for section, revision in used.items()
    )


def republish(
    job_id: str, runtime: MatchIdentityRuntime, *, force: bool = False
) -> dict:
    """Re-run a finished replay's match pass and publish its names.

    Without ``force`` a replay whose published names already used every
    section's current answers is left alone (a duplicate or late delivery).

    Returns:
        The replay's match-pass receipt, or ``{"status": "skipped"}``.

    """
    job = AnalysisJob.objects.select_related("workspace").get(pk=job_id, kind="clip")
    if job.status != "completed" or not named_replay(job):
        # An unfinished replay names everyone when its last section commits.
        return {"status": "skipped"}
    with runtime.processing_store(job.workspace, None) as store:
        marker = directory(store, str(job.pk)) / "run.json"
        hydrate(store, job.workspace, marker)
        record = json.loads(marker.read_text(encoding="utf-8"))
        wide = record.get("match_identity_wide")
        if (
            not force
            and wide
            and wide.get("status") == "completed"
            and not stale(str(job.pk), wide)
        ):
            return {"status": "skipped"}
        record = runtime.publish(store, record)
        atomic_json(marker, record)
        store.publish_artifact(marker.relative_to(store.root).as_posix())
        return record["match_identity_wide"]
