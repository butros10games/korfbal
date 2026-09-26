"""Frozen approved datasets and independent machine-review runs.

No inference result is promoted to a human annotation or a training target here.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
import hashlib
import json
from operator import itemgetter
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uuid

from .boxes import iou
from .clip_models import supports_clips
from .curation import ready
from .store import (
    LABELS,
    MAX_OBJECTS,
    SAFE_ID,
    Store,
    atomic_json,
    ball_review_complete,
    frame_version,
    validate_annotation,
)


KEYPOINTS = ("post_foot",)
POSE_YAML = "kpt_shape: [1, 3]\nflip_idx: [0]\n"
PROFILES = {"people": ("player", "referee", "basket"), "all": LABELS}
MATCH_IOU = 0.5
TEAM_TRANSFER_IOU = 0.7
SPLITS = ("train", "val", "test", "pool")


def digest(path: Path) -> str:
    """Hash a file without materializing a video in memory."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def identifier(value: str) -> str:
    """Validate an artifact name before resolving a path.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError("Use letters, numbers, underscores or hyphens for IDs")
    return value


def artifact(store: Store, kind: str, name: str) -> Path:
    """Resolve only an internal workflow artifact.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    if kind not in {"snapshots", "runs"}:
        raise ValueError("Unknown artifact kind")
    return store.root / "vision" / kind / identifier(name)


def assignments(store: Store) -> dict[str, str]:
    """Read persistent whole-match split assignments."""
    path = store.root / "vision" / "splits.json"
    return json.loads(path.read_text()) if path.exists() else {}


def assign_split(
    store: Store, group: str, split: str, *, override_frozen: bool = False
) -> None:
    """Assign every excerpt together; frozen benchmark groups need an override.

    Older snapshots keep their own frozen splits; an explicit override only
    changes which split future snapshots use.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    if split not in SPLITS:
        raise ValueError("Unknown split")
    with store.transaction():
        groups = {m.get("split_group", m["id"]) for m in store.read()["matches"]}
        if group not in groups:
            raise ValueError("Unknown match group")
        mapping = assignments(store)
        if mapping.get(group, "pool") != split and not override_frozen:
            for path in (store.root / "vision" / "snapshots").glob("*/manifest.json"):
                frozen = json.loads(path.read_text())["splits"]
                if group in frozen and frozen[group] != split:
                    raise ValueError(
                        "This group's split is frozen in a dataset snapshot"
                    )
        mapping[group] = split
        # Split changes never invalidate an in-progress frame correction.
        atomic_json(store.root / "vision" / "splits.json", mapping)


def eligible(frame: dict[str, Any], profile: str) -> bool:
    """Exclude proposals, incomplete reviews and uncertain ball targets."""
    return bool(
        frame["status"] == "approved"
        and frame.get("complete")
        and frame.get("correction") is not None
        and (profile == "people" or ball_review_complete(frame["correction"]))
    )


def inventory(store: Store) -> dict[str, Any]:
    """Summarize readiness without exposing machine paths or loading model weights."""
    mapping = assignments(store)
    data = store.read()
    matches = []
    for match in data["matches"]:
        group = match.get("split_group", match["id"])
        matches.append({
            "id": match["id"],
            "title": match["title"],
            "group": group,
            "split": mapping.get(group, "pool"),
            "synthetic": bool(match.get("synthetic")),
            "approved": sum(eligible(f, "people") for f in match["frames"]),
            "all_classes": sum(eligible(f, "all") for f in match["frames"]),
            "total": len(match["frames"]),
        })
    root = store.root / "vision"
    snapshots = [
        json.loads(p.read_text())
        for p in sorted((root / "snapshots").glob("*/manifest.json"))
    ]
    # The newest snapshot's split per group: moving it changes future comparisons.
    frozen: dict[str, str] = {}
    for snapshot in sorted(snapshots, key=itemgetter("created_at")):
        frozen.update(snapshot.get("splits", {}))
    for match in matches:
        match["frozen_split"] = frozen.get(match["group"])
    runs = [
        json.loads(p.read_text()) for p in sorted((root / "runs").glob("*/run.json"))
    ]
    runs.sort(key=itemgetter("created_at"))
    snapshots.sort(key=itemgetter("created_at"))
    snapshot_classes = {s["id"]: s.get("classes") for s in snapshots}
    for run in runs:
        classes = run.get("classes", snapshot_classes.get(run.get("snapshot")))
        run["clip_compatible"] = (
            run.get("kind") == "train"
            and run.get("status") == "completed"
            and supports_clips(classes)
        )
    return {
        "drafts": {"ready": len(review_queue(store, "latest", data=data))},
        "matches": matches,
        "snapshots": [
            {k: s[k] for k in ("id", "profile", "counts", "created_at")}
            for s in snapshots
        ],
        "runs": [
            {
                k: v
                for k, v in r.items()
                if k
                in {
                    "id",
                    "kind",
                    "status",
                    "snapshot",
                    "created_at",
                    "frames",
                    "error",
                    "metrics",
                    "config",
                    "checkpoint_sha256",
                    "weights_sha256",
                    "clip_compatible",
                }
            }
            for r in runs
        ],
    }


def select_frames(
    data: dict, mapping: dict, profile: str, selection: str
) -> list[tuple[dict, dict]]:
    """Select corrected train/held-out frames without silently omitting stale audits.

    Raises:
        ValueError: A selected audit needs review before freezing.

    """
    if selection not in {"all", "curated"}:
        raise ValueError("Unknown snapshot selection")
    if profile not in PROFILES:
        raise ValueError("Unknown detector profile")
    if selection == "curated":
        candidates = [
            f
            for m in data["matches"]
            for f in m["frames"]
            if f.get("curation", {}).get("selected")
        ]
        if any(not ready(f) or not eligible(f, profile) for f in candidates):
            raise ValueError(
                "Finish or remove incomplete/stale selected audits before freezing"
            )
    selected = [
        (m, f)
        for m in data["matches"]
        if not m.get("synthetic")
        and mapping.get(m.get("split_group", m["id"]), "pool") != "pool"
        for f in m["frames"]
        if eligible(f, profile)
        and (
            selection == "all"
            or mapping.get(m.get("split_group", m["id"])) != "train"
            or (f.get("curation", {}).get("selected") and ready(f))
        )
    ]
    counts = Counter(mapping[m.get("split_group", m["id"])] for m, _ in selected)
    if not counts["train"] or not counts["val"]:
        raise ValueError(
            "Assign separate matches with approved frames "
            "to Training and Validation first"
        )
    return selected


def freeze(
    store: Store, name: str, profile: str = "people", selection: str = "all"
) -> dict[str, Any]:
    """Copy a revision-consistent reviewed dataset; require explicit train and
    validation groups.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    target = artifact(store, "snapshots", name)
    with store.transaction():
        if target.exists():
            raise ValueError("Snapshot already exists; choose a new ID")
        data, mapping = store.read(), assignments(store)
        selected = select_frames(data, mapping, profile, selection)
        classes = PROFILES[profile]
        # Any labelled pole foot turns the snapshot into a keypoint dataset.
        pose = "basket" in classes and any(
            isinstance(obj.get("post_foot"), list)
            for _, frame in selected
            for obj in validate_annotation(frame["correction"])["objects"]
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="snapshot-", dir=target.parent
        ) as temporary:
            root = Path(temporary) / name
            root.mkdir()
            records = []
            image_splits: dict[str, str] = {}
            image_labels: dict[str, list[dict[str, Any]]] = {}
            video_splits: dict[str, str] = {}
            for match, frame in selected:
                split = mapping[match.get("split_group", match["id"])]
                source = store.media(frame["image"])
                image_hash = digest(source)
                video_hash = match.get("video_sha256")
                for key, known in [
                    (image_hash, image_splits),
                    (video_hash, video_splits),
                ]:
                    if key and known.setdefault(key, split) != split:
                        raise ValueError(
                            "Identical source media crosses dataset splits"
                        )
                annotation = validate_annotation(frame["correction"])
                if not reserve_image(image_labels, image_hash, annotation, classes):
                    continue
                stem = hashlib.sha256(
                    f"{match['id']}\0{frame['id']}".encode()
                ).hexdigest()[:24]
                image_name = f"images/{split}/{stem}{source.suffix.lower()}"
                label_name = f"labels/{split}/{stem}.txt"
                image, label = root / image_name, root / label_name
                image.parent.mkdir(parents=True, exist_ok=True)
                label.parent.mkdir(parents=True, exist_ok=True)
                store.reserve_working_bytes(source.stat().st_size + 1024**2)
                shutil.copyfile(source, image)
                lines = []
                for obj in annotation["objects"]:
                    if obj["label"] not in classes:
                        continue
                    x, y, w, h = obj["bbox"]
                    lines.append(
                        f"{classes.index(obj['label'])} {x + w / 2:.8f} "
                        f"{y + h / 2:.8f} {w:.8f} {h:.8f}"
                        + (keypoint(obj) if pose else "")
                    )
                label.write_text("\n".join(lines) + "\n")
                records.append({
                    "match_id": match["id"],
                    "frame_id": frame["id"],
                    "group": match.get("split_group", match["id"]),
                    "split": split,
                    "image": image_name,
                    "label": label_name,
                    "image_sha256": image_hash,
                    "label_sha256": digest(label),
                    "video_sha256": video_hash,
                    "time_seconds": frame["time_seconds"],
                    "frame_version": frame_version(frame),
                    "annotation": annotation,
                    "source": "ai_reviewed"
                    if frame.get("annotation_provenance", {}).get("kind") == "ai"
                    else "human_complete",
                    "annotation_provenance": frame.get("annotation_provenance"),
                    "curation": frame.get("curation")
                    if selection == "curated"
                    else None,
                })
            counts = Counter(r["split"] for r in records)
            frozen_groups = {r["group"]: r["split"] for r in records}
            manifest = {
                "id": name,
                "schema_version": 1,
                "profile": profile,
                "classes": list(classes),
                **({"task": "pose", "keypoints": list(KEYPOINTS)} if pose else {}),
                "created_at": datetime.now(UTC).isoformat(),
                "review_revision": data["revision"],
                "selection": selection,
                "duplicate_images_skipped": len(selected) - len(records),
                "splits": frozen_groups,
                "counts": dict(counts),
                "frames": records,
            }
            atomic_json(root / "manifest.json", manifest)
            # Omit path so a downloaded snapshot resolves relative to its YAML location.
            (root / "data.yaml").write_text(
                "train: images/train\nval: images/val\n"
                + ("test: images/test\n" if counts["test"] else "")
                + "names: "
                + json.dumps(list(classes))
                + "\n"
                + (POSE_YAML if pose else "")
            )
            root.rename(target)
    return manifest


def keypoint(obj: dict[str, Any]) -> str:
    """Pole foot of a basket as a visible keypoint; anything else is unlabelled.

    Visibility 0 carries no training loss, so a basket reviewed before pole feet
    existed (or with a hidden foot) does not teach the model that there is none.
    """
    foot = obj.get("post_foot")
    if obj["label"] == "basket" and isinstance(foot, list):
        return f" {foot[0]:.8f} {foot[1]:.8f} 2"
    return " 0 0 0"


def verify_snapshot(root: Path) -> dict[str, Any]:
    """Refuse changed or escaped training assets before using a frozen dataset.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    for record in manifest["frames"]:
        for kind in ("image", "label"):
            path = (root / record[kind]).resolve()
            if (
                not path.is_relative_to(root)
                or digest(path) != record[f"{kind}_sha256"]
            ):
                raise ValueError("Snapshot integrity check failed")
    return manifest


def compare(
    reference: dict[str, Any],
    prediction: dict[str, Any],
    classes: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Greedy one-to-one diagnostic matches; not an AP or accuracy estimate."""
    counts = {}
    reasons = []
    for label in classes:
        expected = [o for o in reference["objects"] if o["label"] == label]
        proposed = sorted(
            (o for o in prediction["objects"] if o["label"] == label),
            key=itemgetter("confidence"),
            reverse=True,
        )
        unmatched = set(range(len(expected)))
        matched = 0
        for obj in proposed:
            candidate = max(
                unmatched,
                key=lambda j: iou(obj["bbox"], expected[j]["bbox"]),
                default=None,
            )
            if (
                candidate is not None
                and iou(obj["bbox"], expected[candidate]["bbox"]) >= MATCH_IOU
            ):
                unmatched.remove(candidate)
                matched += 1
        counts[label] = {
            "matched": matched,
            "reference_only": len(expected) - matched,
            "model_only": len(proposed) - matched,
        }
        if len(expected) != len(proposed):
            reasons.append(
                f"{label}: count disagreement ({len(expected)} vs {len(proposed)})"
            )
        elif matched != len(expected):
            reasons.append(f"{label}: box placement disagreement")
    if "ball" in classes and not any(
        o["label"] == "ball" for o in prediction["objects"]
    ):
        reasons.append("Ball not found; check surrounding motion")
    score = sum(c["reference_only"] + c["model_only"] for c in counts.values())
    return {"counts": counts, "reasons": reasons, "priority": score + bool(reasons)}


def start_run(
    store: Store, kind: str, **metadata: object
) -> tuple[Path, dict[str, Any]]:
    """Reserve a unique resumable/auditable run directory."""
    name = f"{kind}-{uuid.uuid4().hex[:12]}"
    root = artifact(store, "runs", name)
    root.mkdir(parents=True)
    record = {
        "id": name,
        "kind": kind,
        "status": "running",
        "created_at": datetime.now(UTC).isoformat(),
        **metadata,
    }
    atomic_json(root / "run.json", record)
    return root, record


def proposal_report(store: Store, run: str) -> dict[str, Any]:
    """Combine technical batches, keeping the newest proposal per frame."""
    return {"frames": deepcopy(list(proposal_index(store, run).values()))}


def proposal_index(store: Store, run: str) -> dict[tuple[str, str], dict]:
    """Invalidate parsed proposals when either manifests or predictions change."""
    paths = (
        sorted((store.root / "vision/runs").glob("*/predictions.json"))
        if run == "latest"
        else [artifact(store, "runs", run) / "predictions.json"]
    )
    signature = tuple(
        (
            path,
            path.stat().st_mtime_ns,
            path.stat().st_size,
            (path.parent / "run.json").stat().st_mtime_ns if run == "latest" else 0,
            (path.parent / "run.json").stat().st_size if run == "latest" else 0,
        )
        for path in paths
    )
    return _proposal_index(signature, run == "latest")


@lru_cache(maxsize=4)
def _proposal_index(signature: tuple, latest: bool) -> dict[tuple[str, str], dict]:
    records = {}
    manifests = [
        (
            entry[0],
            json.loads((entry[0].parent / "run.json").read_text()) if latest else {},
        )
        for entry in signature
    ]
    if latest:
        manifests.sort(key=lambda entry: entry[1]["created_at"])
    for path, manifest in manifests:
        if latest and manifest.get("status") != "completed":
            continue
        for record in json.loads(path.read_text())["frames"]:
            records[record["match_id"], record["frame_id"]] = (
                dict(record, run_id=manifest["id"]) if latest else record
            )
    return records


def read_prediction(
    store: Store, run: str, match_id: str, frame_id: str
) -> dict[str, Any]:
    """Read a run proposal independently of mutable review records."""
    return deepcopy(proposal_index(store, run).get((match_id, frame_id), {}))


def merged_proposal(
    frame: dict[str, Any], prediction: dict[str, Any], classes: list[str]
) -> dict[str, Any]:
    """Improve matching boxes without deleting objects another detector missed.

    Original labels remain proposals, not verified truth. Preserve role/identity
    attributes on strong matches and bound the union to the reviewer's capacity.
    """
    annotation: dict[str, Any] = deepcopy(
        frame.get("correction")
        or frame.get("proposal")
        or {"scene": "unknown", "event": "unknown", "objects": [], "notes": ""}
    )
    objects: list[dict[str, Any]] = annotation["objects"]
    used: set[int] = set()
    incoming_objects: list[dict[str, Any]] = prediction["objects"]
    for incoming in sorted(
        incoming_objects, key=itemgetter("confidence"), reverse=True
    ):
        if incoming["label"] not in classes:
            continue
        index = matching_object(objects, incoming, used)
        if index is not None:
            # A generic person detection does not change a referee into a player.
            objects[index]["bbox"] = deepcopy(incoming["bbox"])
            objects[index]["confidence"] = incoming["confidence"]
            if incoming.get("temporal_estimate"):
                objects[index]["temporal_estimate"] = True
            else:
                objects[index].pop("temporal_estimate", None)
            used.add(index)
        elif len(objects) < MAX_OBJECTS:
            objects.append(deepcopy(incoming))
            used.add(len(objects) - 1)
    if any(o["label"] == "ball" for o in objects) and annotation.get(
        "ball_visibility"
    ) in {"outside", "occluded"}:
        annotation["ball_visibility"] = "uncertain"
    return validate_annotation(annotation)


def matching_object(
    objects: list[dict[str, Any]], incoming: dict[str, Any], used: set[int]
) -> int | None:
    """Associate only strong same-role matches or a person's existing referee box."""
    candidates = [
        i
        for i, old in enumerate(objects)
        if i not in used
        and (
            old["label"] == incoming["label"]
            or (incoming["label"] == "player" and old["label"] == "referee")
        )
        and iou(incoming["bbox"], old["bbox"]) >= TEAM_TRANSFER_IOU
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda i: iou(incoming["bbox"], objects[i]["bbox"]))


def review_queue(
    store: Store, run: str, *, data: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Prioritize disagreements while excluding stale proposals and reviewed frames."""
    records = proposal_index(store, run)
    data = store.read() if data is None else data
    current = {(m["id"], f["id"]): f for m in data["matches"] for f in m["frames"]}
    result = []
    for record in records.values():
        frame = current.get((record["match_id"], record["frame_id"]))
        if (
            frame
            and frame["status"] == "pending"
            and record["frame_version"] == frame_version(frame)
        ):
            result.append({
                key: record[key]
                for key in ("match_id", "frame_id", "priority", "reasons")
            })
    return sorted(result, key=itemgetter("priority"), reverse=True)


def audit_dataset(store: Store) -> dict[str, Any]:
    """Measure annotation coverage and proposal disagreements on reviewed examples."""
    matches = []
    totals = {label: Counter() for label in LABELS}
    for match in store.read()["matches"]:
        if match.get("synthetic"):
            continue
        reviewed = [frame for frame in match["frames"] if eligible(frame, "people")]
        distribution: Counter[str] = Counter()
        uncertain_ball = 0
        for frame in reviewed:
            correction = validate_annotation(frame["correction"])
            distribution.update(obj["label"] for obj in correction["objects"])
            uncertain = not ball_review_complete(correction)
            uncertain_ball += uncertain
            if frame.get("proposal") is not None:
                classes = [
                    label for label in LABELS if label != "ball" or not uncertain
                ]
                result = compare(correction, frame["proposal"], classes)
                for label, counts in result["counts"].items():
                    totals[label].update(counts)
        matches.append({
            "match_id": match["id"],
            "approved": len(reviewed),
            "total": len(match["frames"]),
            "objects": dict(distribution),
            "uncertain_ball_frames": uncertain_ball,
        })
    return {
        "matches": matches,
        "proposal_disagreements": {
            label: dict(counts) for label, counts in totals.items()
        },
        "interpretation": (
            "IoU>=0.5 against reviewed corrections: "
            "reference_only is missing or misplaced; model_only is extra or misplaced. "
            "This selected review sample is not a held-out accuracy benchmark."
        ),
    }


def reserve_image(
    known: dict[str, list[dict[str, Any]]],
    image_hash: str,
    annotation: dict[str, Any],
    classes: tuple[str, ...],
) -> bool:
    """Deduplicate identical targets and reject conflicting labels for the same pixels.

    Raises:
        ValueError: If the same image has inconsistent detector targets.

    """
    targets = [
        {"label": obj["label"], "bbox": obj["bbox"]}
        for obj in annotation["objects"]
        if obj["label"] in classes
    ]
    targets.sort(key=itemgetter("label", "bbox"))
    if image_hash not in known:
        known[image_hash] = targets
        return True
    if known[image_hash] != targets:
        raise ValueError("Identical frame images have conflicting training labels")
    return False
