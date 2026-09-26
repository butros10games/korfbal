"""Bounded audit reads and concurrency-safe curation without changing labels."""

from collections import Counter
import json
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q, QuerySet

from apps.video_analysis.engine import curation, vision
from apps.video_analysis.engine.store import (
    ConflictError,
    Store,
    blank_annotation,
    frame_version,
)
from apps.video_analysis.models import Frame, Recording, ReviewAudit, Workspace
from apps.video_analysis.queries import frame_payload
from apps.video_analysis.services.dataset import describe
from apps.video_analysis.services.review import publish_later


PAGE_SIZE = 24
MAX_NOTES = 2000


def sources(store: Store) -> list[dict]:
    """Offer immutable benchmark imports and saved detector runs, without inference."""
    result = [{"id": "original", "title": "Original proposals"}]
    for path in sorted((store.root / "vision/audits").glob("*.json")):
        result.append({
            "id": f"benchmark:{path.stem}",
            "title": f"Benchmark · {path.stem}",
        })
    for path in sorted((store.root / "vision/runs").glob("*/predictions.json")):
        if (
            json.loads((path.parent / "run.json").read_text()).get("status")
            != "completed"
        ):
            continue
        result.append({
            "id": path.parent.name,
            "title": f"Model run · {path.parent.name}",
        })
    return result


def predictions(store: Store, source: str) -> dict[tuple[str, str], dict]:
    """Read one explicit source without silently switching to a newer model.

    Raises:
        TypeError: The source identifier is not text.
        ValueError: The selected model run is not immutable.

    """
    if not isinstance(source, str):
        raise TypeError("Choose a comparison source")
    if source == "original":
        return {}
    if source.startswith("benchmark:"):
        name = vision.identifier(source.removeprefix("benchmark:"))
        report = json.loads((store.root / "vision/audits" / f"{name}.json").read_text())
    else:
        if source == "latest":
            raise ValueError("Choose a specific completed model run")
        run = vision.artifact(store, "runs", source)
        if json.loads((run / "run.json").read_text()).get("status") != "completed":
            raise ValueError("Wait for the model run to finish before auditing")
        return vision.proposal_index(store, source)
    return {(r["match_id"], r["frame_id"]): r for r in report["frames"]}


def detail(frame: Frame, source: str, records: dict, mapping: dict) -> dict:
    """Pair saved model boxes with today's reference, retaining benchmark provenance."""
    raw = frame_payload(frame)
    record = records.get((frame.recording.source_id, frame.source_id), {})
    prediction = (
        raw.get("proposal") if source == "original" else record.get("prediction")
    )
    reference = raw.get("correction") or blank_annotation()
    audit = raw.get("curation", {})
    comparison = curation.compare(prediction or blank_annotation(), reference)
    identity = curation.fingerprint({
        "source": source,
        "prediction": prediction,
        "reference": reference,
    })
    current = audit.get("comparison") == identity and audit.get(
        "frame_version"
    ) == frame_version(raw)
    return dict(
        describe(frame),
        split=mapping.get(
            frame.recording.metadata.get("split_group", frame.recording.source_id),
            "pool",
        ),
        status=frame.status,
        complete=frame.complete,
        has_prediction=prediction is not None,
        has_reference=frame.correction is not None,
        reference_changed=bool(
            record.get("reference") is not None and record["reference"] != reference
        ),
        comparison=identity,
        scores=comparison,
        audit=audit,
        audit_current=current,
        ready=curation.ready(raw),
    )


def totals(frames: QuerySet[Frame], source: str) -> tuple[dict, dict]:
    """Count current audits and their classifications for the selected source."""
    summary = Counter()
    reasons = Counter()
    for frame in frames.iterator():
        raw = frame_payload(frame)
        audit = raw.get("curation", {})
        summary["frames"] += 1
        summary["selected"] += bool(audit.get("selected"))
        summary["ready"] += bool(audit.get("selected") and curation.ready(raw))
        if audit.get("source") == source and audit.get(
            "frame_version"
        ) == frame_version(raw):
            summary["audited"] += bool(audit.get("complete"))
            reasons.update(audit.get("causes", {}).values())
    return dict(summary), dict(reasons)


def listing(workspace: Workspace, store: Store, params: dict[str, Any]) -> dict:
    """Page the source and fetch details for one image.

    Raises:
        ValueError: A filter, cursor or image is invalid.

    """
    source = params.get("source", "original")
    records = predictions(store, source)
    mapping = vision.assignments(store)
    frames = Frame.objects.filter(recording__workspace=workspace).filter(
        Q(metadata__dataset_decision__isnull=True)
        | ~Q(metadata__dataset_decision="removed")
    )
    if source != "original":
        identities = Q(pk__in=[])
        for match_id in {m for m, _ in records}:
            identities |= Q(
                recording__source_id=match_id,
                source_id__in=[f for m, f in records if m == match_id],
            )
        frames = frames.filter(identities)
    if params.get("recording"):
        frames = frames.filter(recording__source_id=params["recording"])
    mode = params.get("filter", "all")
    if mode not in {"all", "selected"}:
        raise ValueError("Unknown audit filter")
    if mode == "selected":
        frames = frames.filter(metadata__curation__selected=True)
    after = int(params.get("after", 0))
    if after < 0:
        raise ValueError("Invalid cursor")
    if params.get("id"):
        frame = frames.select_related("recording").filter(pk=int(params["id"])).first()
        if frame is None:
            raise ValueError("Unknown audit frame")
        return {"frame": detail(frame, source, records, mapping)}
    page = list(
        frames
        .filter(pk__gt=after)
        .select_related("recording")
        .order_by("pk")[: PAGE_SIZE + 1]
    )
    rows = []
    for frame in page[:PAGE_SIZE]:
        item = detail(frame, source, records, mapping)
        item["scores"].pop("rows")
        rows.append(item)
    summary, reasons = totals(frames, source)
    return {
        "summary": dict(summary),
        "causes": dict(reasons),
        "sources": sources(store),
        "recordings": [
            {"id": r.source_id, "title": r.metadata.get("title", r.source_id)}
            for r in Recording.objects.filter(workspace=workspace)
        ],
        "frames": rows,
        "next": page[PAGE_SIZE - 1].pk if len(page) > PAGE_SIZE else None,
    }


@transaction.atomic
def save(workspace: Workspace, store: Store, actor: User, payload: dict) -> dict:
    """Bind audit decisions to exactly the reviewed labels and model output.

    Raises:
        ValueError: A reason, selection, or completion is invalid.
        ConflictError: Another writer changed this frame or its audit.

    """
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    frame = (
        Frame.objects
        .select_for_update(of=("self",))
        .select_related("recording")
        .filter(pk=int(payload["id"]), recording__workspace=workspace)
        .first()
    )
    if frame is None:
        raise ValueError("Unknown audit frame")
    if frame.metadata.get("dataset_decision") == "removed":
        raise ValueError("Restore this image to the dataset first")
    source = payload["source"]
    current = detail(
        frame, source, predictions(store, source), vision.assignments(store)
    )
    if (
        payload.get("frame_version") != current["frame_version"]
        or payload.get("comparison") != current["comparison"]
        or payload.get("audit_revision") != current["audit"].get("revision", 0)
    ):
        raise ConflictError(
            "Labels, predictions or audit changed. Reload before saving."
        )
    tags, causes = payload.get("tags", []), payload.get("causes", {})
    if (
        not isinstance(tags, list)
        or any(not isinstance(t, str) or t not in curation.TAGS for t in tags)
        or not isinstance(causes, dict)
    ):
        raise ValueError("Invalid audit tags")
    rows = current["scores"]["rows"]
    ids = {r["id"] for r in rows}
    if any(
        k not in ids or not isinstance(v, str) or v not in curation.CAUSES
        for k, v in causes.items()
    ):
        raise ValueError("Invalid box classification")
    complete, selected = payload.get("complete", False), payload.get("selected", False)
    if type(complete) is not bool or type(selected) is not bool:
        raise ValueError("Invalid audit decision")
    if selected and current["split"] != "train":
        raise ValueError("Only Training matches can enter the difficult-example batch")
    if complete and (
        not current["has_prediction"]
        or not vision.eligible(frame_payload(frame), "people")
        or any(not r["matched"] and r["id"] not in causes for r in rows)
        or "uncertain" in causes.values()
    ):
        raise ValueError(
            "Approve complete corrected labels and resolve every unmatched box "
            "before completing the audit"
        )
    notes = payload.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES:
        raise ValueError("Invalid audit notes")
    audit = {
        "revision": current["audit"].get("revision", 0) + 1,
        "source": source,
        "comparison": current["comparison"],
        "frame_version": current["frame_version"],
        "tags": sorted(set(tags)),
        "causes": causes,
        "complete": complete,
        "selected": selected,
        "notes": notes,
    }
    frame.metadata = dict(frame.metadata, curation=audit)
    frame.save(update_fields=["metadata"])
    locked.revision += 1
    locked.save(update_fields=["revision"])
    ReviewAudit.objects.create(
        frame=frame,
        actor=actor,
        revision=locked.revision,
        payload={"action": "curation", **audit},
    )
    publish_later(workspace)
    return {
        "frame": detail(
            frame, source, predictions(store, source), vision.assignments(store)
        )
    }
