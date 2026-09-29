"""Identity ground truth: who is who over a clip, labelled independently of any tracker.

A benchmark is a dense run of review frames over one clip interval. Drafts carry
the clip's boxes and teams but never its identities: the reviewer gives every
player a clip-long ID (for example ``A7`` for club A's number 7), so the labels
can score any later tracking run on the same interval without favouring the
run that seeded the boxes.
"""

from dataclasses import dataclass
import math
from typing import Any

from django.db import transaction

from apps.video_analysis.application.ports import FrameExtractor
from apps.video_analysis.engine.clip_evaluation import score_clip
from apps.video_analysis.engine.clip_refinement import refined_frames
from apps.video_analysis.engine.store import (
    Store,
    blank_annotation,
    validate_annotation,
)
from apps.video_analysis.models import AnalysisJob, Frame, Recording, Workspace
from apps.video_analysis.services.clips import result
from apps.video_analysis.services.pipeline_corrections import draft_objects
from apps.video_analysis.services.review import publish_later


DEFAULT_EVERY = 2.0
MAX_FRAMES = 150
TIME_TOLERANCE = 0.001
GRID_TOLERANCE = 1e-6
PEOPLE = {"player", "referee"}


def clip_frames(store: Store, workspace: Workspace, run_id: str) -> tuple[dict, list]:
    """Load a completed clip's receipt and every analyzed frame.

    Raises:
        ValueError: The run is not a completed clip of this workspace.

    """
    job = AnalysisJob.objects.filter(
        workspace=workspace, pk=run_id, kind="clip", status="completed"
    ).first()
    if job is None:
        raise ValueError("Choose a completed clip run")
    receipt = result(store, workspace, run_id, None)
    frames = []
    for chunk in receipt.get("chunks", []):
        frames.extend(result(store, workspace, run_id, chunk["name"])["frames"])
    return {**receipt, "job": job}, frames


def sample_times(options: dict, every: float) -> list[float]:
    """Place times on the clip's own frame grid, so every label aligns with a frame.

    Raises:
        ValueError: The spacing is not a whole number of analyzed frames.

    """
    step = 1 / options.get("fps", 12.5)
    frames = every / step
    if (
        not math.isfinite(every)
        or every <= 0
        or abs(frames - round(frames)) > GRID_TOLERANCE
    ):
        raise ValueError(f"Label every whole number of frames ({step:.2f} s each)")
    start, end = options["start"], options["start"] + options["duration"]
    count = math.floor((end - start) / every - 1e-9) + 1
    if count > MAX_FRAMES:
        raise ValueError(f"Limit a benchmark to {MAX_FRAMES} frames")
    return [round(start + n * every, 3) for n in range(count)]


@dataclass(frozen=True, slots=True)
class BenchmarkRequest:
    """Name a new benchmark over a completed clip run, labelled every ``every`` s."""

    run_id: str
    name: str
    every: float = DEFAULT_EVERY


def create(
    store: Store,
    workspace: Workspace,
    request: BenchmarkRequest,
    *,
    extract_frames: FrameExtractor,
) -> dict[str, Any]:
    """Extract review frames over a clip with identity-free drafts.

    Raises:
        ValueError: The benchmark name exists or frames already use these times.

    """
    run_id, name, every = request.run_id, request.name, request.every
    receipt, frames = clip_frames(store, workspace, run_id)
    job = receipt["job"]
    if Frame.objects.filter(
        recording__workspace=workspace, metadata__identity_benchmark__name=name
    ).exists():
        raise ValueError("A benchmark with this name already exists")
    recording = Recording.objects.get(
        workspace=workspace, source_id=job.payload["match_id"]
    )
    times = sample_times(job.payload["options"], every)
    ids = [f"at-{round(time * 1000):09d}" for time in times]
    taken = list(
        Frame.objects.filter(recording=recording, source_id__in=ids).values_list(
            "source_id", flat=True
        )
    )
    if taken:
        raise ValueError(f"Frames already exist at {', '.join(sorted(taken))}")
    rows = extract_frames(
        store, {**recording.metadata, "id": recording.source_id}, times
    )
    drafts = {}
    for row in rows:
        observed = next(
            (
                frame
                for frame in frames
                if abs(frame["time_seconds"] - row["time_seconds"]) <= TIME_TOLERANCE
            ),
            None,
        )
        objects = (
            draft_objects(observed, receipt, run_id, identities=False)
            if observed
            else []
        )
        drafts[row["id"]] = validate_annotation({
            **blank_annotation(),
            "scene": "live",
            "objects": objects,
        })
    with transaction.atomic():
        locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
        for position, row in enumerate(rows):
            Frame.objects.create(
                recording=recording,
                source_id=row["id"],
                position=round(row["time_seconds"] * 1000),
                proposal=drafts[row["id"]],
                metadata={
                    **{k: v for k, v in row.items() if k != "id"},
                    "model": job.payload.get("model", ""),
                    "proposal_artifact": f"vision/clips/{run_id}/run.json",
                    "identity_benchmark": {
                        "name": name,
                        "source_run": run_id,
                        "index": position,
                    },
                },
            )
        locked.revision += 1
        locked.save(update_fields=["revision"])
    publish_later(workspace)
    return {"name": name, "match_id": recording.source_id, "frames": len(rows)}


def references(workspace: Workspace, name: str) -> list[dict]:
    """Every benchmark frame, reviewed completely, with an ID on each person.

    Raises:
        ValueError: The benchmark is unknown or not fully labelled yet.

    """
    frames = list(
        Frame.objects.filter(
            recording__workspace=workspace, metadata__identity_benchmark__name=name
        ).order_by("position")
    )
    if not frames:
        raise ValueError("Unknown benchmark")
    open_frames = [
        f.source_id
        for f in frames
        if f.status != "approved" or not f.complete or f.correction is None
    ]
    if open_frames:
        raise ValueError(f"{len(open_frames)} frames still need review")
    output = []
    for frame in frames:
        assert frame.correction is not None
        people = [o for o in frame.correction["objects"] if o["label"] in PEOPLE]
        missing = [o for o in people if not o.get("track_id")]
        if missing:
            raise ValueError(f"{frame.source_id}: give every player and referee an ID")
        output.append({
            "time_seconds": frame.metadata["time_seconds"],
            "complete": True,
            "objects": people,
        })
    return output


def evaluate(
    store: Store, workspace: Workspace, name: str, run_id: str
) -> dict[str, Any]:
    """Score one clip run's displayed identities against the benchmark labels.

    Raises:
        ValueError: The run does not cover the labelled interval.

    """
    expected = references(workspace, name)
    receipt, frames = clip_frames(store, workspace, run_id)
    first, last = expected[0]["time_seconds"], expected[-1]["time_seconds"]
    window = [f for f in frames if first - 0.05 <= f["time_seconds"] <= last + 0.05]
    if not window:
        raise ValueError("This run does not cover the benchmark interval")
    resolved = refined_frames(
        [
            {
                **f,
                "objects": [
                    o
                    for o in f["objects"]
                    if o["label"] in PEOPLE and o.get("track_id")
                ],
            }
            for f in window
        ],
        receipt.get("identity_refinement", {}),
    )
    predictions = [
        {
            "time_seconds": f["time_seconds"],
            "objects": [
                {**o, "bbox": o.get("observed_bbox", o["bbox"])} for o in f["objects"]
            ],
        }
        for f in resolved
    ]
    report = score_clip(expected, predictions, scope="complete")
    counts = report["counts"]
    minutes = max(1e-9, (last - first) / 60)
    return {
        "benchmark": name,
        "run": run_id,
        "model": receipt["job"].payload.get("model"),
        "keyframes": len(expected),
        "people": len({o["track_id"] for f in expected for o in f["objects"]}),
        "detected": counts.get("matched", 0) / max(1, counts.get("reference", 0)),
        "idf1": report["idf1_keyframes"],
        "identity_recall": report["identity_recall"],
        "stable_pairs": report["stable_pair_recall"],
        "id_switches": counts.get("id_switches", 0),
        "id_switches_per_minute": counts.get("id_switches", 0) / minutes,
        "wrong_reuses": counts.get("wrong_identity_reuses", 0),
        "predicted_ids": len({
            o["track_id"] for f in predictions for o in f["objects"]
        }),
        "team_mapping": report["team_mapping"],
        "team_correct": counts.get("team_correct", 0)
        / max(1, counts.get("team_reference", 0)),
        "events": report["events"],
    }
