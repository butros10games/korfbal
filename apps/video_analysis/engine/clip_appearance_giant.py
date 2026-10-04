"""Pinned DINOv2 ViT-g/14 with registers as the GPU broadcast descriptor.

The closed-set roster classifier's appearance calibration (98.4% precise from
margin 0.12 on the development clips, ``clip_closed_set_calibration``) was
measured with this descriptor: the official Apache-2.0 checkpoint, the engine's
own crops (``clip_appearance.prepared``), FP16 on a CUDA GPU, and the mean
patch token of three body bands, each normalised (4,608 dimensions). This
module reproduces it and, before a run may use it, compares its output on a
fixed synthetic frame with reference descriptors computed by that measurement's
own code. A model that does not reproduce them is not loaded, so the calibration
keyed by ``FINGERPRINT`` only ever applies to verified descriptors.

The 1.1-billion-parameter model needs a GPU (``KORFBAL_CLIP_DEVICE=cuda``);
its checkpoint is downloaded once into the disposable vision cache and verified
against its pinned digest, like the DINOv2-S export.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import math
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

from . import clip_device
from .clip_appearance import (
    BANDS,
    DOWNLOAD_TIMEOUT,
    GIANT_ARCHITECTURE,
    HEIGHT,
    PATCH,
    REPOSITORY,
    WIDTH,
    prepared,
)
from .clip_signals import modules
from .vision import digest


if TYPE_CHECKING:
    from numpy.typing import NDArray

ARCHITECTURE = GIANT_ARCHITECTURE
CHECKPOINT_URL = (
    "https://dl.fbaipublicfiles.com/dinov2/dinov2_vitg14/"
    "dinov2_vitg14_reg4_pretrain.pth"
)
CHECKPOINT_SHA256 = "746ecb8c6301c645c5c855be91687d274587d6e48fdaec4a729753160b34a283"
VERSION = 1
# Crops per GPU call; a frame rarely has more than 20 players and referees.
BATCH = 64
REFERENCE = Path(__file__).with_name("models") / "dinov2-vitg14-reg-reference.npz"
# FP16 on different GPUs and drivers agrees to about 1e-4; a wrong model,
# crop or band layout lands far below this.
MIN_REFERENCE_COSINE = 0.999


def recipe() -> dict[str, Any]:
    """Everything that defines the descriptor values, independent of versions."""
    return {
        "version": VERSION,
        "repository": REPOSITORY,
        "architecture": ARCHITECTURE,
        "checkpoint_sha256": CHECKPOINT_SHA256,
        "shape": [HEIGHT, WIDTH],
        "bands": [list(band) for band in BANDS],
        "precision": "fp16",
        "crops": "clip_appearance.prepared",
    }


FINGERPRINT = hashlib.sha256(json.dumps(recipe(), sort_keys=True).encode()).hexdigest()


def reference_frame() -> NDArray[Any]:
    """Build a fixed 720p BGR frame from closed-form arithmetic (no random stream)."""
    _, np = modules()
    y, x = np.mgrid[0:720, 0:1280].astype(np.float64)
    channels = [
        128
        + 90 * np.sin(x / (17 + 6 * c) + c) * np.cos(y / (23 + 4 * c) - c)
        + 30 * np.sin((x + 2 * y) / (5 + c))
        for c in range(3)
    ]
    return np.clip(np.stack(channels, axis=-1), 0, 255).astype(np.uint8)


# Small (upscaled), medium and large (downscaled) boxes, one at the frame edge.
REFERENCE_BOXES = (
    (0.10, 0.20, 0.03, 0.11),
    (0.40, 0.30, 0.06, 0.28),
    (0.62, 0.05, 0.14, 0.62),
    (0.93, 0.50, 0.07, 0.45),
)


def checkpoint(cache: Path) -> Path:
    """Download the pinned checkpoint once and verify it on every load.

    Raises:
        ValueError: The checkpoint does not match its pinned digest.

    """
    request = importlib.import_module("urllib.request")
    root = cache / f"appearance-giant-{FINGERPRINT[:16]}"
    root.mkdir(parents=True, exist_ok=True)
    target = root / "checkpoint.pth"
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.is_file() or digest(target) != CHECKPOINT_SHA256:
            staged = root / "checkpoint.partial"
            with (
                request.urlopen(CHECKPOINT_URL, timeout=DOWNLOAD_TIMEOUT) as response,
                staged.open("wb") as handle,
            ):
                while block := response.read(1 << 22):
                    handle.write(block)
            if digest(staged) != CHECKPOINT_SHA256:
                staged.unlink()
                raise ValueError("Giant checkpoint does not match its pinned digest")
            staged.replace(target)
    return target


class GiantAppearance:
    """Describe player crops with the verified FP16 model on the GPU."""

    def __init__(self, cache: Path) -> None:
        """Load the pinned model and refuse it unless it reproduces the reference.

        Raises:
            ValueError: No GPU is configured or the output differs from the reference.

        """
        if clip_device.device() != "cuda":
            raise ValueError("The giant descriptor runs only on a CUDA worker")
        torch = importlib.import_module("torch")
        if not torch.cuda.is_available():
            raise ValueError("CUDA is unavailable to PyTorch")
        self.torch = torch
        weights = checkpoint(cache)
        model = torch.hub.load(
            REPOSITORY,
            ARCHITECTURE,
            pretrained=False,
            trust_repo=True,
            verbose=False,
        )
        model.load_state_dict(torch.load(weights, map_location="cpu"))
        self.model = model.half().cuda().eval()
        self.device = "cuda"
        self.seconds = 0.0
        self.cosine = self.verify()
        if self.cosine < MIN_REFERENCE_COSINE:
            raise ValueError("Giant descriptor does not reproduce its reference")
        self.seconds = 0.0

    def embed(self, batch: NDArray[Any]) -> NDArray[Any]:
        """Banded FP16 descriptors for a prepared N x 3 x 224 x 112 batch."""
        _, np = modules()
        torch = self.torch
        rows, columns = HEIGHT // PATCH, WIDTH // PATCH
        output = []
        started = time.monotonic()
        with torch.inference_mode():
            for start in range(0, len(batch), BATCH):
                crops = torch.from_numpy(batch[start : start + BATCH]).cuda().half()
                tokens = self.model.forward_features(crops)["x_norm_patchtokens"]
                grid = tokens.float().reshape(len(crops), rows, columns, -1)
                bands = torch.stack(
                    [grid[:, top:bottom].mean((1, 2)) for top, bottom in BANDS], 1
                )
                bands = torch.nn.functional.normalize(bands, dim=-1)
                output.append(
                    bands.reshape(len(crops), -1).cpu().numpy().astype(np.float16)
                )
        self.seconds += time.monotonic() - started
        return np.concatenate(output)

    def describe(
        self, image: NDArray[Any], boxes: list[list[float]]
    ) -> dict[int, NDArray[Any]]:
        """Return a descriptor per usable normalised `[x, y, w, h]` box."""
        batch, kept = prepared(image, boxes)
        if batch is None:
            return {}
        return dict(zip(kept, self.embed(batch), strict=True))

    def verify(self) -> float:
        """Smallest cosine between this model's and the reference descriptors."""
        _, np = modules()
        with np.load(REFERENCE, allow_pickle=False) as stored:
            expected = stored["descriptors"].astype(np.float64)
        found = self.describe(reference_frame(), [list(b) for b in REFERENCE_BOXES])
        if sorted(found) != list(range(len(expected))):
            return -math.inf
        actual = np.stack([found[i] for i in sorted(found)]).astype(np.float64)
        cosine = (actual * expected).sum(1) / np.maximum(
            np.linalg.norm(actual, axis=1) * np.linalg.norm(expected, axis=1), 1e-12
        )
        return float(cosine.min())

    def receipt(self) -> dict[str, Any]:
        """Describe the verified descriptor for the run receipt and calibration."""
        return {
            "status": "enabled",
            "sha256": FINGERPRINT,
            "model": ARCHITECTURE,
            "checkpoint_sha256": CHECKPOINT_SHA256,
            "device": self.device,
            "reference_cosine": round(self.cosine, 6),
        }
