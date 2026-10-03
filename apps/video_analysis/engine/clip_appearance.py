"""Appearance descriptors for player crops, used to tell teammates apart.

A pinned DINOv2 ViT-S/14 describes the head, torso and legs of a crop. The
descriptor is not an identity by itself: `clip_linking` fits a clip-specific
space on it. The model is prepared once per worker as a fingerprinted ONNX
export in the disposable vision cache, like the CPU detector export.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING, Any, cast

from .clip_signals import modules
from .store import atomic_json
from .vision import digest


if TYPE_CHECKING:
    from numpy.typing import NDArray

REPOSITORY = "facebookresearch/dinov2:7764ea0f912e53c92e82eb78a2a1631e92725fc8"
ARCHITECTURE = "dinov2_vits14"
CHECKPOINT_URL = (
    "https://dl.fbaipublicfiles.com/dinov2/dinov2_vits14/dinov2_vits14_pretrain.pth"
)
CHECKPOINT_SHA256 = "b938bf1bc15cd2ec0feacfe3a1bb553fe8ea9ca46a7e1d8d00217f29aef60cd9"
EXPORT_VERSION = 1
HEIGHT, WIDTH, PATCH = 224, 112, 14
# Rows of the 16-row patch grid that describe head, torso and legs.
BANDS = ((0, 5), (5, 10), (10, 16))
MEAN = (0.485, 0.456, 0.406)
DEVIATION = (0.229, 0.224, 0.225)
MIN_CROP = 2
DOWNLOAD_TIMEOUT = 120
THREADS = 2


def recipe() -> dict:
    """Invalidate the export when the model, layout or export tools change."""
    return {
        "version": EXPORT_VERSION,
        "repository": REPOSITORY,
        "architecture": ARCHITECTURE,
        "checkpoint_sha256": CHECKPOINT_SHA256,
        "shape": [HEIGHT, WIDTH],
        "bands": [list(band) for band in BANDS],
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("torch", "onnx", "onnxruntime")
        },
    }


def export(target: Path, workspace: Path) -> None:
    """Write the banded descriptor model, verifying the pinned checkpoint.

    Raises:
        ValueError: The downloaded checkpoint is not the pinned one.

    """
    torch = importlib.import_module("torch")
    request = importlib.import_module("urllib.request")
    checkpoint = workspace / "checkpoint.pth"
    with (
        request.urlopen(CHECKPOINT_URL, timeout=DOWNLOAD_TIMEOUT) as response,
        checkpoint.open("wb") as handle,
    ):
        while block := response.read(1 << 20):
            handle.write(block)
    if digest(checkpoint) != CHECKPOINT_SHA256:
        raise ValueError("Appearance checkpoint does not match its pinned digest")
    backbone = torch.hub.load(
        REPOSITORY, ARCHITECTURE, pretrained=False, trust_repo=True, verbose=False
    )
    backbone.load_state_dict(torch.load(checkpoint, map_location="cpu"))
    rows, columns = HEIGHT // PATCH, WIDTH // PATCH

    class Banded(torch.nn.Module):
        """Average patch tokens per body band and normalise each band."""

        def __init__(self) -> None:
            super().__init__()
            self.backbone = backbone

        def forward(self, crops: object) -> object:
            tokens = self.backbone.forward_features(crops)["x_norm_patchtokens"]
            grid = tokens.reshape(tokens.shape[0], rows, columns, tokens.shape[-1])
            return torch.cat(
                [
                    torch.nn.functional.normalize(
                        grid[:, top:bottom].mean((1, 2)), dim=1
                    )
                    for top, bottom in BANDS
                ],
                dim=1,
            )

    torch.onnx.export(
        Banded().eval(),
        (torch.zeros(1, 3, HEIGHT, WIDTH),),
        str(target),
        input_names=["crops"],
        output_names=["descriptors"],
        dynamic_axes={"crops": {0: "count"}, "descriptors": {0: "count"}},
        opset_version=17,
        dynamo=False,
    )


def cached_export(cache: Path) -> Path:
    """Publish a complete, fingerprinted export or reuse a verified one."""
    wanted = recipe()
    key = hashlib.sha256(json.dumps(wanted, sort_keys=True).encode()).hexdigest()
    root = cache / f"appearance-{key[:16]}"
    root.mkdir(parents=True, exist_ok=True)
    target, marker = root / "model.onnx", root / "export.json"
    with (root / ".lock").open("a") as lock:
        # Wait for a concurrent run: verifying or exporting takes seconds, and
        # a run without the model would silently fall back to weaker identities.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_file() and marker.is_file():
            try:
                record = json.loads(marker.read_text())
                if record["recipe"] == wanted and record["sha256"] == digest(target):
                    return target
            except (ValueError, KeyError, TypeError):
                pass  # An incomplete disposable cache is rebuilt, never trusted.
        with tempfile.TemporaryDirectory(prefix="export-", dir=root) as temporary:
            staged = Path(temporary) / "model.onnx"
            export(staged, Path(temporary))
            staged.replace(target)
        atomic_json(marker, {"recipe": wanted, "sha256": digest(target)})
    return target


class Appearance:
    """Describe player crops in batches with the exported model."""

    def __init__(self, model: Path) -> None:
        """Open one bounded CPU session."""
        runtime = importlib.import_module("onnxruntime")
        options = runtime.SessionOptions()
        options.intra_op_num_threads = THREADS
        options.inter_op_num_threads = 1
        self.session = runtime.InferenceSession(
            str(model), options, providers=["CPUExecutionProvider"]
        )
        self.seconds = 0.0

    def describe(
        self, image: NDArray[Any], boxes: list[list[float]]
    ) -> dict[int, NDArray[Any]]:
        """Return a descriptor per usable normalised `[x, y, w, h]` box."""
        cv, np = modules()
        height, width = image.shape[:2]
        crops, kept = [], []
        for index, (x, y, w, h) in enumerate(boxes):
            left, top = max(0, round(x * width)), max(0, round(y * height))
            right = min(width, round((x + w) * width))
            bottom = min(height, round((y + h) * height))
            if right - left < MIN_CROP or bottom - top < MIN_CROP:
                continue
            crop = image[top:bottom, left:right]
            shrink = crop.shape[0] > HEIGHT
            resized = cv.resize(
                crop,
                (WIDTH, HEIGHT),
                interpolation=cv.INTER_AREA if shrink else cv.INTER_CUBIC,
            )
            crops.append((resized[:, :, ::-1] / 255.0 - MEAN) / DEVIATION)
            kept.append(index)
        if not crops:
            return {}
        batch = np.stack(crops).transpose(0, 3, 1, 2).astype(np.float32)
        started = time.monotonic()
        output = self.session.run(None, {"crops": batch})[0]
        self.seconds += time.monotonic() - started
        return dict(zip(kept, output, strict=True))


def beside_cache(cache: Path) -> tuple[Appearance | None, dict[str, Any]]:
    """Load the descriptor model, or report why linking stays unavailable."""
    if os.environ.get("KORFBAL_CLIP_LINKING", "1") == "0":
        return None, {"status": "disabled"}
    try:
        configured = os.environ.get("KORFBAL_CLIP_APPEARANCE_MODEL")
        model = Path(configured) if configured else cached_export(cache)
        return Appearance(model), {"status": "enabled", "sha256": digest(model)}
    except Exception as error:  # noqa: BLE001 - linking is optional evidence.
        return None, {"status": "unavailable", "reason": type(error).__name__}


def describe_players(
    appearance: Appearance, image: NDArray[Any], players: list[dict]
) -> dict[int, NDArray[Any]]:
    """Describe every observed player box of one frame."""
    boxes = [
        cast("list[float]", player.get("observed_bbox") or player["bbox"])
        for player in players
    ]
    return appearance.describe(image, boxes)
