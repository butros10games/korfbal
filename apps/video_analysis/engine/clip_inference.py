"""CPU clip inference with checkpoint-bound, disposable ONNX exports.

Training and review proposals continue to use the original PyTorch checkpoint.
Exports preserve rectangular letterboxing, FP32 weights and Ultralytics decoding.
"""

from __future__ import annotations

from collections import OrderedDict
import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, cast

from .store import atomic_json
from .training import Detector, detector
from .vision import digest


CPU_THREADS = 2
EXPORT_VERSION = 1
MAX_SESSIONS = 2


def export_recipe(weights: Path, shape: tuple[int, int]) -> dict:
    """Invalidate derived artifacts when weights, shape or export tools change."""
    return {
        "version": EXPORT_VERSION,
        "weights_sha256": digest(weights),
        "shape": list(shape),
        "opset": 17,
        "precision": "fp32",
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("torch", "ultralytics", "onnx", "onnxslim", "onnxruntime")
        },
    }


def cached_export(weights: Path, cache: Path, shape: tuple[int, int]) -> Path:
    """Publish a complete, fingerprinted export without touching the checkpoint.

    Raises:
        ValueError: Another process is already preparing this exact export.

    """
    recipe = export_recipe(weights, shape)
    key = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
    root = cache / key
    root.mkdir(parents=True, exist_ok=True)
    target, marker = root / "model.onnx", root / "export.json"
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "This CPU export is being prepared by another run"
            ) from error
        if target.is_file() and marker.is_file():
            try:
                record = json.loads(marker.read_text())
                if record["recipe"] == recipe and record["sha256"] == digest(target):
                    return target
            except (ValueError, KeyError, TypeError):
                pass  # An incomplete disposable cache is rebuilt, never trusted.
        with tempfile.TemporaryDirectory(prefix="export-", dir=root) as temporary:
            source = Path(temporary) / "weights.pt"
            shutil.copyfile(weights, source)
            if digest(source) != recipe["weights_sha256"]:
                raise ValueError("Checkpoint changed during CPU export")
            model = cast("Any", detector(str(source)))
            exported = Path(
                model.export(
                    format="onnx",
                    imgsz=shape,
                    device="cpu",
                    batch=1,
                    opset=recipe["opset"],
                    simplify=True,
                    verbose=False,
                )
            )
            os.replace(exported, target)
        atomic_json(marker, {"recipe": recipe, "sha256": digest(target)})
    return target


class CpuDetector:
    """Keep one bounded session, using the original checkpoint as provenance."""

    def __init__(self, weights: str, cache: Path) -> None:
        """Load class metadata; export lazily for the actual decoded aspect ratio."""
        model = cast("Any", detector(weights))
        self.names = model.names
        self.ckpt_path = weights
        self.cache = cache
        self.stride = int(model.model.stride.max())
        self.end2end = bool(getattr(model.model, "end2end", False))
        self.session: Any = None
        self.shape: tuple[int, int] | None = None
        self.sessions: OrderedDict[tuple[int, int], tuple[Any, dict]] = OrderedDict()
        self.inference_info: dict[str, Any] = {
            "backend": "onnxruntime",
            "precision": "fp32",
        }

    def prepare(self, shape: tuple[int, int]) -> None:
        """Limit execution to the worker's CPU quota, including background pools."""
        if self.session is not None and self.shape == shape:
            return
        if shape in self.sessions:
            self.session, info = self.sessions[shape]
            self.sessions.move_to_end(shape)
            self.shape = shape
            self.inference_info.update(info)
            return
        runtime = importlib.import_module("onnxruntime")
        exported = cached_export(Path(self.ckpt_path), self.cache, shape)
        options = runtime.SessionOptions()
        options.intra_op_num_threads = CPU_THREADS
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = runtime.InferenceSession(
            str(exported), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.shape = shape
        self.inference_info.update(
            runtime_version=runtime.__version__,
            export_sha256=digest(exported),
            input_shape=list(shape),
            cpu_threads=CPU_THREADS,
        )
        self.sessions[shape] = self.session, self.inference_info.copy()
        # Retain the normal frame and one fixed crop shape, never one per region.
        while len(self.sessions) > MAX_SESSIONS:
            self.sessions.popitem(last=False)

    def predict(self, source: object, **options: object) -> list[Any]:
        """Use the same colour order, padding, NMS and box coordinates as PyTorch."""
        source = cast("Any", source)
        np = importlib.import_module("numpy")
        torch = importlib.import_module("torch")
        augment = importlib.import_module("ultralytics.data.augment")
        nms = importlib.import_module("ultralytics.utils.nms")
        ops = importlib.import_module("ultralytics.utils.ops")
        results = importlib.import_module("ultralytics.engine.results")
        size = options["imgsz"]
        image = augment.LetterBox((size, size), auto=True, stride=self.stride)(
            image=source
        )
        shape = tuple(image.shape[:2])
        self.prepare(shape)
        # Export setup can reset PyTorch's pool; NMS must respect the quota too.
        torch.set_num_threads(CPU_THREADS)
        tensor = (
            np.ascontiguousarray(image.transpose(2, 0, 1)[::-1][None], dtype=np.float32)
            / 255
        )
        prediction = self.session.run(
            None, {self.session.get_inputs()[0].name: tensor}
        )[0]
        boxes = nms.non_max_suppression(
            torch.from_numpy(prediction),
            options["conf"],
            0.7,
            max_det=options["max_det"],
            end2end=self.end2end,
        )[0]
        boxes[:, :4] = ops.scale_boxes(shape, boxes[:, :4], source.shape)
        return [results.Results(source, path="", names=self.names, boxes=boxes)]


def clip_detector(weights: str, cache: Path) -> CpuDetector | Detector:
    """Allow an explicit operational rollback without changing request recipes.

    Raises:
        ValueError: The server runtime setting is unsupported.

    """
    backend = os.environ.get("KORFBAL_CLIP_BACKEND", "onnx")
    if backend == "torch":
        return detector(weights)
    if backend != "onnx":
        raise ValueError("KORFBAL_CLIP_BACKEND must be onnx or torch")
    return CpuDetector(weights, cache)
