"""Appearance descriptors for player crops, used to tell teammates apart.

A pinned DINOv2 ViT-S/14 describes the head, torso and legs of a crop. By
default it carries a korfbal adapter (`models/`, pinned by digest): low-rank
weight updates learned from player tracklets in public korfbal broadcasts,
merged into the pretrained weights before export; `models/README.md` records
its data and licences. Without the adapter the plain pretrained model is used.
The descriptor is not an identity by itself: `clip_linking` fits a
clip-specific space on it. The model is prepared once per worker as a
fingerprinted ONNX export in the disposable vision cache, like the CPU detector
export. It runs on the CPU unless the worker opts in to CUDA
(``clip_device``, ``KORFBAL_CLIP_DEVICE=cuda``).
`KORFBAL_CLIP_APPEARANCE_MODEL` points at any other ONNX file with the same
contract: input `crops` (N x 3 x 224 x 112, ImageNet-normalised RGB), output
`descriptors` (N x D).
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING, Any, cast

from . import clip_device
from .clip_signals import modules
from .store import atomic_json
from .vision import digest


if TYPE_CHECKING:
    from numpy.typing import NDArray

    from .clip_appearance_giant import GiantAppearance

REPOSITORY = "facebookresearch/dinov2:7764ea0f912e53c92e82eb78a2a1631e92725fc8"
ARCHITECTURE = "dinov2_vits14"
CHECKPOINT_URL = (
    "https://dl.fbaipublicfiles.com/dinov2/dinov2_vits14/dinov2_vits14_pretrain.pth"
)
CHECKPOINT_SHA256 = "b938bf1bc15cd2ec0feacfe3a1bb553fe8ea9ca46a7e1d8d00217f29aef60cd9"
ADAPTER = Path(__file__).with_name("models") / "appearance-korfbal-v1.npz"
ADAPTER_NAME = "dinov2_vits14+korfbal-v1"
ADAPTER_SHA256 = "40c4d238b5728e2cd1536909e8af04e50eb5aceb577ba66706d48d7c470a1f9c"
EXPORT_VERSION = 2
HEIGHT, WIDTH, PATCH = 224, 112, 14
# Rows of the 16-row patch grid that describe head, torso and legs.
BANDS = ((0, 5), (5, 10), (10, 16))
MEAN = (0.485, 0.456, 0.406)
DEVIATION = (0.229, 0.224, 0.225)
MIN_CROP = 2
DOWNLOAD_TIMEOUT = 120
THREADS = 2
# Broadcast descriptor setting that selects the pinned GPU DINOv2-g/14 model.
GIANT_ARCHITECTURE = "dinov2_vitg14_reg"


def adapter() -> Path | None:
    """Return the shipped korfbal adapter, or None when it is missing or altered."""
    try:
        return ADAPTER if digest(ADAPTER) == ADAPTER_SHA256 else None
    except OSError:
        return None


def recipe(*, tuned: bool) -> dict:
    """Invalidate the export when the model, layout or export tools change."""
    return {
        "version": EXPORT_VERSION,
        "repository": REPOSITORY,
        "architecture": ARCHITECTURE,
        "checkpoint_sha256": CHECKPOINT_SHA256,
        "adapter_sha256": ADAPTER_SHA256 if tuned else None,
        "shape": [HEIGHT, WIDTH],
        "bands": [list(band) for band in BANDS],
        "packages": {
            name: clip_device.package_version(name)
            for name in ("torch", "onnx", "onnxruntime")
        },
    }


def adapt(state: dict[str, Any], path: Path) -> None:
    """Add the adapter's low-rank updates (`<weight>.B @ <weight>.A`) and its norm."""
    _, np = modules()
    torch = importlib.import_module("torch")
    with np.load(path, allow_pickle=False) as delta:
        for key in delta.files:
            if key.endswith(".A"):
                name = key.removesuffix(".A")
                update = delta[f"{name}.B"].astype(np.float32) @ delta[key].astype(
                    np.float32
                )
                state[f"{name}.weight"] += torch.from_numpy(update)
            elif not key.endswith(".B"):
                state[key] = torch.from_numpy(delta[key].astype(np.float32))


def export(target: Path, workspace: Path, tuned: Path | None = None) -> None:
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
    state = torch.load(checkpoint, map_location="cpu")
    if tuned is not None:
        adapt(state, tuned)
    backbone.load_state_dict(state)
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


def cached_export(cache: Path, tuned: Path | None = None) -> Path:
    """Publish a complete, fingerprinted export or reuse a verified one."""
    wanted = recipe(tuned=tuned is not None)
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
            export(staged, Path(temporary), tuned)
            staged.replace(target)
        atomic_json(marker, {"recipe": wanted, "sha256": digest(target)})
    return target


def prepared(
    image: NDArray[Any], boxes: list[list[float]]
) -> tuple[NDArray[Any] | None, list[int]]:
    """Cut, resize and normalise usable normalised `[x, y, w, h]` boxes.

    Returns:
        An N x 3 x 224 x 112 float32 batch (or None) and the kept box indices.

    """
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
        return None, []
    return np.stack(crops).transpose(0, 3, 1, 2).astype(np.float32), kept


class Appearance:
    """Describe player crops in batches with the exported model."""

    def __init__(self, model: Path) -> None:
        """Open one bounded session (CPU, or CUDA when the worker opts in)."""
        self.session = clip_device.session(model, THREADS)
        self.device = clip_device.used(self.session)
        self.seconds = 0.0

    def describe(
        self, image: NDArray[Any], boxes: list[list[float]]
    ) -> dict[int, NDArray[Any]]:
        """Return a descriptor per usable normalised `[x, y, w, h]` box."""
        batch, kept = prepared(image, boxes)
        if batch is None:
            return {}
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
        if configured:
            model, name = Path(configured), "configured"
        else:
            tuned = adapter()
            model = cached_export(cache, tuned)
            name = ADAPTER_NAME if tuned else ARCHITECTURE
        appearance = Appearance(model)
    except Exception as error:  # noqa: BLE001 - linking is optional evidence.
        return None, {"status": "unavailable", "reason": type(error).__name__}
    return appearance, {
        "status": "enabled",
        "model": name,
        "sha256": digest(model),
        "device": appearance.device,
    }


def beside_broadcast(
    cache: Path | None = None,
) -> tuple[Appearance | GiantAppearance | None, dict[str, Any] | None]:
    """Load the optional descriptor for operated (broadcast) camera footage.

    A re-identification model helps on broadcast footage but not on a fixed
    eye-level club camera, where players are about 100 pixels tall. When one is
    configured both descriptors are computed and the linker chooses per clip.
    The value is an ONNX file, or ``dinov2_vitg14_reg`` for the pinned GPU
    DINOv2-g/14 descriptor (``clip_appearance_giant``).
    """
    configured = os.environ.get("KORFBAL_CLIP_BROADCAST_APPEARANCE_MODEL")
    if not configured or os.environ.get("KORFBAL_CLIP_LINKING", "1") == "0":
        return None, None
    try:
        if configured == GIANT_ARCHITECTURE and cache is None:
            return None, {"status": "unavailable", "reason": "no_cache"}
        if configured == GIANT_ARCHITECTURE:
            module = importlib.import_module(f"{__package__}.clip_appearance_giant")
            giant = module.GiantAppearance(cache)
            return giant, giant.receipt()
        model = Path(configured)
        appearance = Appearance(model)
        return appearance, {
            "status": "enabled",
            "sha256": digest(model),
            "device": appearance.device,
        }
    except Exception as error:  # noqa: BLE001 - optional evidence.
        return None, {"status": "unavailable", "reason": type(error).__name__}


def describe_players(
    appearance: Appearance | GiantAppearance, image: NDArray[Any], players: list[dict]
) -> dict[int, NDArray[Any]]:
    """Describe every observed player box of one frame."""
    boxes = [
        cast("list[float]", player.get("observed_bbox") or player["bbox"])
        for player in players
    ]
    return appearance.describe(image, boxes)
