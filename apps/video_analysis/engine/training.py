"""Optional Ultralytics adapter for local training, evaluation and review proposals.

Create the optional runtime with:
python -m scripts.python.korfbal_vision_environment NEW_ENV [--cpu]
Then use NEW_ENV/bin/python -m scripts.python.korfbal_vision --help.

Ultralytics code/weights have separate licensing terms. This adapter never rents
compute or uploads footage. CPU is the default; select --device 0 on a GPU host.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import importlib
import importlib.metadata
from itertools import zip_longest
import json
import math
from operator import itemgetter
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Protocol, cast

from . import ball_crops, numbers, temporal
from .coverage import dataset_report
from .keypoints import post_feet
from .recovery import training_lease
from .store import (
    Store,
    atomic_json,
    blank_annotation,
    frame_version,
    validate_annotation,
)
from .vision import (
    artifact,
    assignments,
    compare,
    digest,
    merged_proposal,
    start_run,
    verify_snapshot,
)


@dataclass(frozen=True)
class RunOptions:
    """Bounded local execution settings shared by the CLI and model adapter."""

    device: str = "cpu"
    epochs: int = 30
    imgsz: int = 960
    batch: int = 2
    workers: int = 2
    cache: str = "off"
    optimizer: str = "auto"
    learning_rate: float | None = None
    freeze: int = 0
    patience: int | None = None
    close_mosaic: int = 10
    seed: int = 42
    confidence: float = 0.25
    spacing: float = 2
    limit: int = 100
    match_id: str | None = None
    split: str = "val"
    ball_tiles: bool = False
    tracker: str = "botsort"
    temporal_mode: str = "refine"
    evaluate_temporal: bool = False


class Detector(Protocol):
    """Minimal optional detector-runtime interface."""

    names: dict[int, str]
    ckpt_path: str

    def track(self, source: object, **kwargs: object) -> list[Any]:
        """Return tracking records for consecutive frames."""
        ...

    def train(self, **kwargs: object) -> object:
        """Run model training."""
        ...

    def predict(self, source: object, **kwargs: object) -> list[Any]:
        """Return backend prediction records."""
        ...


MAX_EPOCHS = 300
MAX_BATCH = 64
MIN_IMAGE_SIZE = 320
MAX_IMAGE_SIZE = 1920
MAX_PROPOSALS = 500
MIN_CONFIDENCE = 0.01
MAX_SPACING = 3600
MAX_WORKERS = 16
MIN_LEARNING_RATE = 0.000001
MAX_LEARNING_RATE = 0.1
MAX_FROZEN_LAYERS = 10
MAX_CLOSE_MOSAIC = 50
PROPOSAL_VERSION = 4
PRETRAINED_WEIGHTS = ("yolo26n.pt", "yolo26s.pt", "yolo26m.pt")
CLASS_ALIASES = {"person": "player", "sports ball": "ball"}


def detector(weights: str) -> Detector:
    """Load the optional runtime only for a requested model operation.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    try:
        module = importlib.import_module("ultralytics")
    except ImportError as error:
        raise ValueError(
            "Create a headless runtime: python -m scripts.python."
            "korfbal_vision_environment NEW_ENV --cpu. "
            "Then run with NEW_ENV/bin/python; omit --cpu for GPU training."
        ) from error
    if weights in PRETRAINED_WEIGHTS:
        cache = Path.home() / ".cache" / "korfbal-vision" / "models"
        cache.mkdir(parents=True, exist_ok=True)
        weights = str(cache / weights)
    module.settings.update({"sync": False})
    return cast("Detector", module.YOLO(weights))


def pose_model(model: Any, weights: str) -> Any:  # noqa: ANN401
    """Continue from a box detector as a keypoint model of the same size.

    The pole-foot keypoint needs a pose head; the backbone and box head of the
    given weights are transferred, so earlier training is not thrown away.
    """
    if getattr(model, "task", "detect") == "pose":
        return model
    module = importlib.import_module("ultralytics")
    scale = (getattr(model.model, "yaml", None) or {}).get("scale") or "n"
    return module.YOLO(f"yolo26{scale}-pose.yaml").load(
        getattr(model, "ckpt_path", None) or weights
    )


def environment() -> dict[str, Any]:
    """Record exact installed versions and source revision for reproducibility."""
    versions = {}
    for name in (
        "ultralytics",
        "torch",
        "numpy",
        "opencv-python",
        "opencv-python-headless",
        "onnx",
        "onnxruntime",
    ):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    revision_file = Path(__file__).resolve().parents[3] / "REVISION"
    revision = revision_file.read_text().strip() if revision_file.exists() else ""
    git = shutil.which("git")
    if not revision and git:
        result = subprocess.run(
            [git, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        )
        revision = result.stdout.strip()
    return {
        "python": sys.version,
        "packages": versions,
        "git_revision": revision,
    }


def weights_record(weights: str, model: Detector) -> dict[str, Any]:
    """Fingerprint a resolved checkpoint, including downloaded pretrained assets."""
    resolved = Path(getattr(model, "ckpt_path", weights) or weights)
    return {
        "weights": resolved.name,
        "weights_sha256": digest(resolved) if resolved.is_file() else None,
    }


def predict_image(
    model: Detector, image: Path, device: str, imgsz: int, confidence: float
) -> tuple[dict[str, Any], list[str]]:
    """Translate generic or Korfbal detector output to the reviewer schema.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    names = {int(k): v for k, v in model.names.items()}
    mapping = {
        i: CLASS_ALIASES.get(name, name)
        for i, name in names.items()
        if name in {"person", "sports ball", "player", "referee", "basket", "ball"}
    }
    if not mapping:
        raise ValueError(
            "Checkpoint has no supported classes; use a person or Korfbal detector"
        )
    result = model.predict(
        str(image),
        device=device,
        imgsz=imgsz,
        conf=confidence,
        classes=list(mapping),
        max_det=80,
        verbose=False,
    )[0]
    annotation = blank_annotation()
    if result.boxes is not None:
        boxes = result.boxes.xyxyn.cpu().tolist()
        classes = result.boxes.cls.cpu().tolist()
        scores = result.boxes.conf.cpu().tolist()
        feet = post_feet(result, len(boxes))
        for box, cls, score, foot in zip(boxes, classes, scores, feet, strict=True):
            label = mapping.get(int(cls))
            if label is None:
                continue
            x1, y1, x2, y2 = [min(1.0, max(0.0, float(v))) for v in box]
            if x2 <= x1 or y2 <= y1:
                continue
            annotation["objects"].append({
                "label": label,
                "bbox": [x1, y1, x2 - x1, y2 - y1],
                "confidence": float(score),
                # A model proposal only; reviewers confirm or move it.
                **(
                    {"post_foot": foot}
                    if label == "basket" and foot and foot[1] >= y1
                    else {}
                ),
            })
    return validate_annotation(annotation), sorted(set(mapping.values()))


def predict_with_options(
    model: Detector, image: Path, options: RunOptions
) -> tuple[dict[str, Any], list[str]]:
    """Apply an explicitly selected inference experiment to the same source pixels."""
    predicted, classes = predict_image(
        model, image, options.device, options.imgsz, options.confidence
    )
    if options.ball_tiles:
        predicted = ball_crops.augment(model, image, options, predicted)
    return predicted, classes


def train(
    store: Store, snapshot: str, weights: str, options: RunOptions | None = None
) -> dict[str, Any]:
    """Train only on verified complete reviewed labels and retain failed-run
    diagnostics.

    Raises:
        ValueError: If the operation or input is invalid.
        KeyboardInterrupt: If the user interrupts training.

    """
    config_options = options or RunOptions()
    patience = (
        config_options.epochs
        if config_options.patience is None
        else config_options.patience
    )
    device = config_options.device
    epochs = config_options.epochs
    imgsz = config_options.imgsz
    batch = config_options.batch
    seed = config_options.seed
    if (
        not 1 <= epochs <= MAX_EPOCHS
        or not (batch == -1 or 1 <= batch <= MAX_BATCH)
        or not MIN_IMAGE_SIZE <= imgsz <= MAX_IMAGE_SIZE
    ):
        raise ValueError(
            "Training bounds: epochs 1-300, batch -1 or 1-64, image size 320-1920"
        )
    validate_options(config_options)
    if batch == -1 and device == "cpu":
        raise ValueError(
            "Automatic batch sizing requires a CUDA device; use --batch 2 on CPU"
        )
    dataset = artifact(store, "snapshots", snapshot)
    coverage = dataset_report(store, snapshot)
    manifest = json.loads((dataset / "manifest.json").read_text())
    root, run = start_run(
        store,
        "train",
        snapshot=snapshot,
        classes=manifest["classes"],
        dataset_sha256=digest(dataset / "manifest.json"),
        coverage=coverage,
        config={
            "epochs": epochs,
            "imgsz": imgsz,
            "batch": batch,
            "seed": seed,
            "device": device,
            "workers": config_options.workers,
            "cache": config_options.cache,
            "amp": True,
            "patience": patience,
            "optimizer": config_options.optimizer,
            "learning_rate": config_options.learning_rate,
            "freeze": config_options.freeze,
            "close_mosaic": config_options.close_mosaic,
        },
    )
    with training_lease(root):
        started = time.monotonic()
        try:
            model = detector(weights)
            if manifest.get("task") == "pose":
                model = pose_model(model, weights)
                run["task"] = "pose"
            run.update(environment=environment(), **weights_record(weights, model))
            atomic_json(root / "run.json", run)
            # Absolute paths in a run-local YAML leave the portable snapshot untouched.
            config = {
                "path": str(dataset),
                "train": "images/train",
                "val": "images/val",
                "names": manifest["classes"],
                **(
                    {"kpt_shape": [len(manifest["keypoints"]), 3], "flip_idx": [0]}
                    if manifest.get("task") == "pose"
                    else {}
                ),
            }
            atomic_json(root / "data.yaml", config)
            model.train(
                data=str(root / "data.yaml"),
                epochs=epochs,
                imgsz=imgsz,
                batch=batch,
                device=device,
                seed=seed,
                deterministic=True,
                workers=config_options.workers,
                cache=False if config_options.cache == "off" else config_options.cache,
                amp=True,
                project=str(root),
                name="fit",
                exist_ok=False,
                # Existing recipes honor their epoch request. Controlled trials
                # can stop earlier while retaining the best checkpoint.
                patience=patience,
                optimizer=config_options.optimizer,
                freeze=config_options.freeze or None,
                close_mosaic=config_options.close_mosaic,
                # Best/last already persist each epoch; periodic copies overflow
                # the bounded remote result archive on longer, larger runs.
                save_period=-1,
                hsv_h=0.0,
                hsv_s=0.0,
                flipud=0.0,
                **(
                    {"lr0": config_options.learning_rate}
                    if config_options.learning_rate is not None
                    else {}
                ),
            )
            best = root / "fit" / "weights" / "best.pt"
            if not best.is_file():
                raise ValueError("Trainer completed without a best checkpoint")
            run.update(
                status="completed",
                checkpoint="fit/weights/best.pt",
                checkpoint_sha256=digest(best),
            )
            if shirt_numbers(manifest):
                run["numbers"] = number_reader(dataset, best.parent, device, seed)
        except (Exception, KeyboardInterrupt) as error:
            run.update(
                status="interrupted"
                if isinstance(error, KeyboardInterrupt)
                else "failed",
                error=f"{type(error).__name__}: {error}",
            )
            raise
        finally:
            run["elapsed_seconds"] = round(time.monotonic() - started, 3)
            atomic_json(root / "run.json", run)
    return run


def shirt_numbers(manifest: dict[str, Any]) -> bool:
    """Whether any reviewed player in the snapshot carries a shirt-number label."""
    return any(
        "shirt_number" in obj
        for record in manifest["frames"]
        for obj in record["annotation"]["objects"]
    )


def number_reader(
    dataset: Path, weights: Path, device: str, seed: int
) -> dict[str, Any]:
    """Train the shirt-number reader beside the detector checkpoint.

    The detector is already saved; a reader failure is recorded, never raised.
    """
    try:
        return numbers.train_reader(
            dataset, weights, numbers.Fit(device=device, seed=seed)
        )
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        return {"status": "failed", "error": f"{type(error).__name__}: {error}"}


def proposals(
    store: Store, weights: str, options: RunOptions | None = None
) -> dict[str, Any]:
    """Run bounded diverse proposals outside held-out groups; never mutate review
    frames.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    config_options = options or RunOptions()
    limit = config_options.limit
    device = config_options.device
    imgsz = config_options.imgsz
    confidence = config_options.confidence
    spacing = config_options.spacing
    if (
        not 1 <= limit <= MAX_PROPOSALS
        or not MIN_CONFIDENCE <= confidence <= 1
        or not 0 <= spacing <= MAX_SPACING
    ):
        raise ValueError(
            "Proposal bounds: limit 1-500, confidence 0.01-1, spacing 0-3600 seconds"
        )
    validate_options(config_options)
    candidates = select_candidates(store, config_options)
    if not candidates:
        raise ValueError(
            "No untouched pending frames outside validation/test; "
            "import or sample training clips first"
        )
    root, run = start_run(
        store,
        "propose",
        frames=0,
        config={
            "imgsz": imgsz,
            "confidence": confidence,
            "spacing": spacing,
            "device": device,
            "ball_tiles": config_options.ball_tiles,
            "tracker": config_options.tracker,
            "temporal_mode": config_options.temporal_mode,
        },
    )
    records = []
    try:
        model = detector(weights)
        run.update(environment=environment(), **weights_record(weights, model))
        run["context_sources"] = {
            match["id"]: match.get("video_sha256") for match, _ in candidates
        }
        run["context_manifest_sha256"] = temporal.manifest_digest(store)
        key = inference_key(run, config_options)
        cached = cached_frames(store, key)
        run.update(
            inference_key=key, reused_frames=0, proposal_version=PROPOSAL_VERSION
        )
        temporal_model = None
        for match, frame in candidates:
            image_hash = digest(store.media(frame["image"]))
            identity = (match["id"], frame["id"], frame_version(frame), image_hash)
            if identity in cached:
                run["reused_frames"] += 1
                continue
            if len(records) >= limit:
                break
            predicted, classes = predict_with_options(
                model, store.media(frame["image"]), config_options
            )
            window = temporal.context(store, match, frame)
            if window and temporal_model is None:
                temporal_model = detector(weights)
            predicted, context_info = temporal.predict(
                temporal_model or model,
                store.media(frame["image"]),
                window,
                config_options,
                predicted,
            )
            reference = frame.get("proposal") or blank_annotation()
            record = {
                "match_id": match["id"],
                "frame_id": frame["id"],
                "frame_version": frame_version(frame),
                "image_sha256": image_hash,
                "classes": classes,
                "prediction": predicted,
                "temporal": context_info,
                "suggestion": merged_proposal(frame, predicted, classes),
                **compare(reference, predicted, classes),
            }
            records.append(record)
            # Checkpoint partial results; readers never see truncated JSON.
            atomic_json(
                root / "predictions.json", {"run": run["id"], "frames": records}
            )
            run["frames"] = len(records)
            atomic_json(root / "run.json", run)
        run["status"] = "completed"
    except Exception as error:
        run.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        atomic_json(root / "run.json", run)
    return run


def evaluate(
    store: Store, snapshot: str, weights: str, options: RunOptions | None = None
) -> dict[str, Any]:
    """Measure fixed-threshold detection precision/recall with one-to-one IoU
    matches.

    Raises:
        ValueError: If the operation or input is invalid.

    """
    config_options = options or RunOptions()
    split = config_options.split
    device = config_options.device
    imgsz = config_options.imgsz
    confidence = config_options.confidence
    validate_options(config_options)
    if split not in {"val", "test"}:
        raise ValueError("Evaluate on validation or test only")
    root_data = artifact(store, "snapshots", snapshot)
    manifest = verify_snapshot(root_data)
    frames = [f for f in manifest["frames"] if f["split"] == split]
    if not frames:
        raise ValueError("No reviewed frames in the requested evaluation split")
    root, run = start_run(
        store,
        "evaluate",
        snapshot=snapshot,
        dataset_sha256=digest(root_data / "manifest.json"),
        split=split,
        config={
            "imgsz": imgsz,
            "confidence": confidence,
            "iou": 0.5,
            "device": device,
            "ball_tiles": config_options.ball_tiles,
            "temporal": config_options.evaluate_temporal,
            "tracker": config_options.tracker,
            "temporal_mode": config_options.temporal_mode,
        },
    )
    started = time.monotonic()
    try:
        model = detector(weights)
        run.update(environment=environment(), **weights_record(weights, model))
        totals = {label: Counter() for label in manifest["classes"]}
        records = []
        matches = (
            {m["id"]: m for m in store.read()["matches"]}
            if config_options.evaluate_temporal
            else {}
        )
        temporal_model = detector(weights) if config_options.evaluate_temporal else None
        for frame in frames:
            prediction, supported = predict_with_options(
                model, root_data / frame["image"], config_options
            )
            context_info = {"mode": "single-frame", "context_frames": 0}
            if temporal_model is not None:
                prediction, context_info = temporal.predict(
                    temporal_model,
                    root_data / frame["image"],
                    evaluation_context(store, frame, matches),
                    config_options,
                    prediction,
                )
            result = compare(frame["annotation"], prediction, manifest["classes"])
            for label, counts in result["counts"].items():
                totals[label].update(counts)
            records.append({
                "frame_id": frame["frame_id"],
                "match_id": frame["match_id"],
                "temporal": context_info,
                **result,
            })
        metrics = {}
        for label, count in totals.items():
            tp, fn, fp = count["matched"], count["reference_only"], count["model_only"]
            metrics[label] = {
                **count,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
                "supported": label in supported,
            }
        run.update(
            status="completed",
            metrics=metrics,
            frames=len(frames),
            metric_definition=(
                "Greedy confidence-ordered one-to-one IoU>=0.5; "
                "fixed threshold, not mAP"
            ),
        )
        atomic_json(root / "evaluation.json", {"metrics": metrics, "frames": records})
    except Exception as error:
        run.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        run["elapsed_seconds"] = round(time.monotonic() - started, 3)
        atomic_json(root / "run.json", run)
    return run


def evaluation_context(store: Store, frame: dict, matches: dict) -> list[Path]:
    """Bind temporal inputs to the frozen reference image, not a stale cache.

    Raises:
        ValueError: If source metadata is absent or pixels changed.

    """
    match = matches.get(frame["match_id"])
    if match is None:
        raise ValueError("Temporal evaluation needs the source recording and frame")
    source = next((f for f in match["frames"] if f["id"] == frame["frame_id"]), None)
    if source is None:
        raise ValueError("Temporal evaluation needs the source recording and frame")
    if digest(store.media(source["image"])) != frame["image_sha256"]:
        raise ValueError("Temporal evaluation source pixels changed")
    return temporal.context(store, match, source)


def validate_options(options: RunOptions) -> None:
    """Reject invalid inference and loader settings before loading model weights.

    Raises:
        ValueError: If an execution setting is invalid.

    """
    if (
        not math.isfinite(options.confidence)
        or not MIN_CONFIDENCE <= options.confidence <= 1
    ):
        raise ValueError("Confidence must be between 0.01 and 1")
    if not MIN_IMAGE_SIZE <= options.imgsz <= MAX_IMAGE_SIZE:
        raise ValueError("Image size must be between 320 and 1920")
    if not 0 <= options.workers <= MAX_WORKERS or options.cache not in {
        "off",
        "disk",
        "ram",
    }:
        raise ValueError("Use workers 0-16 and cache off, disk or ram")
    if (
        type(options.ball_tiles) is not bool
        or type(options.evaluate_temporal) is not bool
    ):
        raise ValueError("Inference experiment switches must be booleans")
    if options.tracker not in {"botsort", "bytetrack", "ocsort"}:
        raise ValueError("Use tracker botsort, bytetrack or ocsort")
    if options.temporal_mode not in {"refine", "recover"}:
        raise ValueError("Use temporal mode refine or recover")
    validate_recipe(options)


def validate_recipe(options: RunOptions) -> None:
    """Reject ignored settings or unbounded training recipes before allocation.

    Raises:
        ValueError: If a controlled training option is invalid.

    """
    if options.optimizer not in {"auto", "AdamW", "SGD"}:
        raise ValueError("Use optimizer auto, AdamW or SGD")
    if options.learning_rate is not None and (
        not math.isfinite(options.learning_rate)
        or not MIN_LEARNING_RATE <= options.learning_rate <= MAX_LEARNING_RATE
    ):
        raise ValueError("Learning rate must be between 0.000001 and 0.1")
    if (options.optimizer == "auto") != (options.learning_rate is None):
        raise ValueError(
            "Set both an explicit optimizer and learning rate, or use auto"
        )
    if type(options.freeze) is not int or not 0 <= options.freeze <= MAX_FROZEN_LAYERS:
        raise ValueError("Freeze must be an integer between 0 and 10 layers")
    if options.patience is not None and (
        type(options.patience) is not int or not 1 <= options.patience <= options.epochs
    ):
        raise ValueError("Patience must be an integer between 1 and the epoch limit")
    if (
        type(options.close_mosaic) is not int
        or not 0 <= options.close_mosaic <= MAX_CLOSE_MOSAIC
    ):
        raise ValueError("Close mosaic must be an integer between 0 and 50 epochs")


def balanced_candidates(
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Share the annotation budget across recordings with different offsets."""
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for candidate in candidates:
        groups.setdefault(candidate[0]["id"], []).append(candidate)
    return [
        item
        for row in zip_longest(*groups.values())
        for item in row
        if item is not None
    ]


def inference_key(run: dict[str, Any], options: RunOptions) -> str | None:
    """Invalidate cached predictions when weights or inference settings change."""
    if not run.get("weights_sha256"):
        return None
    return hashlib.sha256(
        json.dumps(
            {
                "weights": run["weights_sha256"],
                "imgsz": options.imgsz,
                "confidence": options.confidence,
                "version": PROPOSAL_VERSION,
                "context_sources": run.get("context_sources"),
                "context_manifest": run.get("context_manifest_sha256"),
                "device": options.device,
                **(
                    {
                        "experiments": {
                            "ball_tiles": options.ball_tiles,
                            "tracker": options.tracker,
                            "temporal_mode": options.temporal_mode,
                        }
                    }
                    if options.ball_tiles
                    or options.tracker != "botsort"
                    or options.temporal_mode != "refine"
                    else {}
                ),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def cached_frames(store: Store, key: str | None) -> set[tuple[str, str, str, str]]:
    """Reuse only fully written predictions with exact image and review fingerprints."""
    cached = set()
    if key is None:
        return cached
    for path in (store.root / "vision" / "runs").glob("*/run.json"):
        run = json.loads(path.read_text())
        predictions = path.parent / "predictions.json"
        if run.get("inference_key") != key or not predictions.exists():
            continue
        cached.update(
            (
                record["match_id"],
                record["frame_id"],
                record["frame_version"],
                record["image_sha256"],
            )
            for record in json.loads(predictions.read_text())["frames"]
        )
    return cached


def select_candidates(
    store: Store, options: RunOptions
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Select spaced untouched frames while protecting evaluation and human drafts."""
    match_id, spacing = options.match_id, options.spacing
    data, splits = store.read(), assignments(store)
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for match in data["matches"]:
        if match.get("synthetic") or (match_id and match["id"] != match_id):
            continue
        if splits.get(match.get("split_group", match["id"]), "pool") in {"val", "test"}:
            continue
        last = -float("inf")
        frames: list[dict[str, Any]] = match["frames"]
        for frame in sorted(frames, key=itemgetter("time_seconds")):
            if (
                frame["status"] != "pending"
                or frame.get("correction") is not None
                or frame["time_seconds"] - last < spacing
            ):
                continue
            candidates.append((match, frame))
            last = frame["time_seconds"]
    # Broadcast clocks differ: round-robin whole recordings, not absolute times.
    return balanced_candidates(candidates)
