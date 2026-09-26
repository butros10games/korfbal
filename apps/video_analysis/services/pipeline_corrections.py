"""Seed correction sequences from the exact completed replay being inspected."""

from apps.video_analysis.engine.clip_refinement import refined_frames
from apps.video_analysis.engine.store import (
    Store,
    blank_annotation,
    validate_annotation,
)
from apps.video_analysis.models import ReviewPipeline
from apps.video_analysis.services.clips import result


TIME_TOLERANCE = 0.001


def clip_drafts(
    run: ReviewPipeline, store: Store, candidates: list[dict]
) -> list[dict]:
    """Retain displayed identities as suggestions, scoped to their immutable clip."""
    run_id = run.recipe.get("clip_run_id")
    if not run_id or not candidates:
        return []
    receipt = result(store, run.workspace, run_id, None)
    times = {row["id"]: int(row["id"].removeprefix("at-")) / 1000 for row in candidates}
    frames = []
    for chunk in receipt.get("chunks", []):
        if any(
            chunk["start"] - TIME_TOLERANCE <= time <= chunk["end"] + TIME_TOLERANCE
            for time in times.values()
        ):
            frames.extend(result(store, run.workspace, run_id, chunk["name"])["frames"])
    drafts = []
    for row in candidates:
        observed = next(
            (
                frame
                for frame in frames
                if abs(frame["time_seconds"] - times[row["id"]]) <= TIME_TOLERANCE
            ),
            None,
        )
        if observed is None:
            continue
        tracked = [obj for obj in observed["objects"] if obj.get("track_id")]
        other = [obj for obj in observed["objects"] if not obj.get("track_id")]
        resolved = refined_frames(
            [{**observed, "objects": tracked}], receipt.get("identity_refinement", {})
        )[0]
        objects = []
        for obj in [*resolved["objects"], *other]:
            if obj["label"] not in {"player", "referee", "ball", "basket"}:
                continue
            item = {
                "label": obj["label"],
                "bbox": obj.get("observed_bbox", obj["bbox"]),
                "confidence": obj.get("confidence", 0),
                "temporal_estimate": bool(
                    obj.get("temporal_estimate") or obj.get("identity_uncertain")
                ),
                "team": obj.get("team", "unknown")
                if obj["label"] == "player"
                else "unknown",
            }
            if obj.get("track_id"):
                item["track_id"] = f"{run_id}-{obj['track_id']}"
            item.update({
                attribute: obj[attribute]
                for attribute in ("shirt_number", "post_foot")
                if attribute in obj
            })
            objects.append(item)
        annotation = {**blank_annotation(), "scene": "live", "objects": objects}
        drafts.append({
            **row,
            "prediction": validate_annotation(annotation),
            "source_clip": run_id,
        })
    return drafts
