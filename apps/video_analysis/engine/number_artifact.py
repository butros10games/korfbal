"""Independent, checksummed shirt readers with frozen tracklet calibration."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from . import clip_device, number_patch
from .clip_signals import modules
from .number_evidence import AgreementVotes, EpisodeVotes, best, tempered
from .numbers import NumberReader, ShirtNumbers, beside, softmax
from .vision import digest


VERSION = 1
PARSEQ_SIZE = (128, 32)
MIN_CALIBRATION_TRACKLETS = 20
TARGET_PRECISION = 0.99


class SceneTextReader:
    """ONNX PARSeq reader with a separate torso legibility head."""

    def __init__(self, path: Path, tokenizer: dict[str, Any]) -> None:
        """Open an already checksummed model; no upstream Python is imported."""
        self.session = session(path)
        self.input_name = self.session.get_inputs()[0].name
        self.tokenids = tokenizer["digits"]
        self.eos = tokenizer["eos"]
        self.sha256 = digest(path)

    def distributions(self, crops: list) -> list[dict[str, float]]:
        """Decode 0-99; letters, extra digits and hidden shirts remain unknown."""
        if not crops:
            return []
        cv, np = modules()
        images = np.stack([cv.resize(c, PARSEQ_SIZE) for c in crops])
        images = images[..., ::-1].transpose(0, 3, 1, 2).astype(np.float32)
        outputs = self.session.run(None, {self.input_name: images / 127.5 - 1})
        tokens, legibility = (np.asarray(o) for o in outputs)
        tokens = np.exp(tokens - tokens.max(axis=-1, keepdims=True))
        tokens /= tokens.sum(axis=-1, keepdims=True)
        legibility = softmax(legibility)
        result = []
        for probabilities, flag in zip(tokens, legibility, strict=True):
            distribution = {}
            for n in range(100):
                text = str(n)
                value = float(flag[1])
                for i, digit in enumerate(text):
                    value *= float(probabilities[i, self.tokenids[digit]])
                value *= float(probabilities[len(text), self.eos])
                distribution[text] = value
            distribution["unknown"] = max(0.0, 1 - sum(distribution.values()))
            result.append(distribution)
        return result

    def read(self, crops: list) -> list[tuple[str | None, float]]:
        """Compatibility API for tools that inspect one hard crop reading."""
        return [best(d) for d in self.distributions(crops)]


def session(path: Path) -> Any:  # noqa: ANN401 - lazy onnxruntime session
    """Four-thread ONNX session (CUDA when the worker opts in), like the others."""
    return clip_device.session(path, 4)


class PatchReader(SceneTextReader):
    """Localise the printed number first, then read only that patch.

    Input crops are upper bodies (``number_patch.upper_body``). A one-class
    detector finds the number; without a confident box the crop is hidden
    (all mass unknown). The patch keeps its aspect ratio inside the reader's
    128x32 input instead of squashing a whole torso into a word image.
    """

    def __init__(
        self,
        path: Path,
        tokenizer: dict[str, Any],
        patch_path: Path,
        patch_threshold: float,
    ) -> None:
        """Open both checksummed models."""
        super().__init__(path, tokenizer)
        self.detector = session(patch_path)
        self.detector_input = self.detector.get_inputs()[0].name
        self.patch_threshold = patch_threshold
        self.patch_sha256 = digest(patch_path)

    @staticmethod
    def crop(image: Any, box: list[float]) -> Any | None:  # noqa: ANN401
        """Upper-body crop of a normalized person box."""
        return number_patch.upper_body(image, box)

    def patches(self, crops: list) -> list[tuple[Any, float, list[float]] | None]:
        """Return patch, detector confidence and box per crop; None when hidden."""
        if not crops:
            return []
        batch, undo = number_patch.detector_input(crops)
        output = self.detector.run(None, {self.detector_input: batch})[0]
        return [
            (number_patch.reader_patch(crop, box), conf, box)
            if conf >= self.patch_threshold
            else None
            for crop, (box, conf) in zip(
                crops, number_patch.best_boxes(output, crops, undo), strict=True
            )
        ]

    def distributions(self, crops: list) -> list[dict[str, float]]:
        """Joint 0-99 probabilities; crops without a localised number are unknown."""
        found = self.patches(crops)
        visible = [p for p in found if p is not None]
        read = iter(super().distributions([p[0] for p in visible]))
        return [next(read) if p is not None else {"unknown": 1.0} for p in found]


def calibration_threshold(record: dict[str, Any]) -> float | None:
    """Enable hard votes only with explicit independent-tracklet validation."""
    threshold = record.get("threshold")
    accepted = record.get("accepted", 0)
    correct = record.get("correct", 0)
    if (
        record.get("unit") != "tracklet"
        or accepted < MIN_CALIBRATION_TRACKLETS
        or not 0 <= correct <= accepted
        or correct / accepted < TARGET_PRECISION
    ):
        return None
    if (
        not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        return None
    return float(threshold)


def model_file(record: dict[str, Any], path: Path) -> Path:
    """Resolve a supported manifest's model beside it.

    Raises:
        ValueError: Manifest version or file location is unsupported.

    """
    if record["version"] != VERSION:
        raise ValueError("Unsupported number artifact manifest version")
    model_path = (path.parent / record["file"]).resolve()
    if not model_path.is_relative_to(path.parent.resolve()):
        raise ValueError("Number artifact must be beside its manifest")
    return model_path


def reader_model(
    record: dict[str, Any], path: Path, manifest: Path
) -> NumberReader | SceneTextReader:
    """Instantiate one supported checksummed reader format.

    Raises:
        ValueError: Manifest names an unsupported model format.

    """
    if record["format"] == "digit-pytorch-v1":
        return NumberReader(path)
    if record["format"] == "parseq-onnx-v1":
        return SceneTextReader(path, record["tokenizer"])
    if record["format"] == "parseq-patch-onnx-v1":
        patch = model_file({**record, "file": record["patch"]["file"]}, manifest)
        if digest(patch) != record["patch"]["sha256"]:
            raise ValueError("Number patch detector checksum mismatch")
        return PatchReader(
            path, record["tokenizer"], patch, float(record["patch"]["threshold"])
        )
    raise ValueError("Unsupported number artifact format")


def aggregator(record: dict[str, Any], temperature: float) -> Any:  # noqa: ANN401
    """Per-track vote factory named by the manifest (default: episode peaks).

    Raises:
        ValueError: Manifest names an unknown aggregation.

    """
    method = record.get("aggregation", {"method": "episode-peaks"})
    if method["method"] == "episode-peaks":
        return lambda: EpisodeVotes(temperature=temperature)
    if method["method"] == "agreement":
        support = int(method["min_support"])
        if support < 1:
            raise ValueError("Agreement aggregation needs a positive support")
        return lambda: AgreementVotes(min_support=support)
    raise ValueError("Unsupported number aggregation")


def artifact(path: Path) -> tuple[ShirtNumbers | None, dict[str, Any]]:
    """Load a frozen manifest independent of the detector's training snapshot.

    Invalid explicit artifacts fail closed; they never silently select a reader
    from a different detector run. Uncalibrated models expose soft evidence only.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        model_path = model_file(record, path)
        if digest(model_path) != record["sha256"]:
            return None, {"status": "checksum_mismatch"}
        model = reader_model(record, model_path, path)
        calibration = record["calibration"]
        threshold = (
            calibration_threshold(calibration)
            if record.get("hard_numbers_approved") is True
            else None
        )
        sampler = ShirtNumbers(
            model,
            threshold,
            temperature=float(calibration["temperature"]),
            tracklet_calibration=True,
            votes=aggregator(record, float(calibration["temperature"])),
        )
        tempered({"unknown": 1.0}, sampler.temperature)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, {"status": "artifact_invalid", "error": str(error)}
    return sampler, {
        "status": "enabled" if threshold is not None else "evidence_only",
        "source": "independent_artifact",
        "manifest_sha256": digest(path),
        "reader_sha256": model.sha256,
        **(
            {"patch_sha256": model.patch_sha256}
            if isinstance(model, PatchReader)
            else {}
        ),
        "aggregation": record.get("aggregation", {"method": "episode-peaks"}),
        "calibration": calibration,
        # Roster-restricted anchors for the closed-set classifier are approved
        # separately from open-set hard numbers (clip_number_anchors).
        "roster_anchors": record.get("roster_anchors"),
    }


def load(weights: str) -> tuple[ShirtNumbers | None, dict[str, Any]]:
    """Respect the disable switch, prefer an explicit artifact, then legacy."""
    if os.environ.get("KORFBAL_CLIP_NUMBERS", "1") == "0":
        return None, {"status": "disabled"}
    selected = os.environ.get("KORFBAL_CLIP_NUMBERS_ARTIFACT")
    return artifact(Path(selected)) if selected else beside(weights)
