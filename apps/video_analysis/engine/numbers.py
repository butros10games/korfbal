"""Read players' shirt numbers from their torso, digit by digit.

Teammates wear identical kit; the number is what tells them apart for a whole
match. The reader predicts three things from a torso crop: whether a number is
readable, the tens digit (or none) and the units digit. Reading digits instead
of whole numbers lets it read a number that never appeared in the labels.

Training uses reviewed `shirt_number` labels from a frozen snapshot: digits, or
"hidden" for a visible but unreadable shirt. Unreviewed players are never used,
so a turned back never teaches "no number". Rendered synthetic numbers on shirt
colours pad the few real labels, but a reader trained on them alone reads "74"
on almost every real shirt; validation therefore uses real labels only, and the
reader only reports numbers above a threshold calibrated on them to be right at
least 98% of the time. The reader is a plain PyTorch state dict: GPU training
environments have no ONNX runtime, and a few crops per second are cheap on the
CPU clip worker, which already has PyTorch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import time
from typing import Any

from .clip_signals import modules
from .store import atomic_json
from .vision import digest


VERSION = 1
WIDTH, HEIGHT = 64, 96
TORSO = (-0.05, 0.08, 1.05, 0.62)  # left, top, right, bottom as box fractions
MIN_BOX_PIXELS = 48
MIN_READABLE_LABELS = 50
TENS = 11  # blank, then 0-9
UNITS = 10
EPOCHS = 30
BATCH = 64
LEARNING_RATE = 1e-3
SYNTHETIC_PER_REAL = 1
MIN_SYNTHETIC = 500
TARGET_PRECISION = 0.98
MIN_CALIBRATION_READS = 20
THRESHOLDS = tuple(round(0.5 + 0.01 * k, 2) for k in range(50))
MAX_CROPS = 50_000
FILE = "numbers.pt"
READ_INTERVAL = 0.4
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
SHIRTS = (
    ((40, 40, 200), (255, 255, 255)),
    ((255, 255, 255), (30, 30, 30)),
    ((60, 140, 40), (255, 255, 255)),
    ((200, 90, 20), (255, 255, 255)),
    ((30, 200, 230), (20, 20, 20)),
    ((20, 20, 20), (255, 255, 255)),
    ((255, 255, 255), (40, 40, 200)),
)
NUMBERED_SHARE = 0.8
BLUR_SHARE = 0.5
FONTS = (0, 2, 3, 4)  # OpenCV Hershey simplex, duplex, complex, triplex


def torso(image: Any, box: list[float]) -> Any | None:  # noqa: ANN401
    """Cut the shirt area of a normalized person box, or None when too small."""
    cv, _ = modules()
    h, w = image.shape[:2]
    x, y, bw, bh = box
    if bh * h < MIN_BOX_PIXELS:
        return None
    left = max(0, round((x + TORSO[0] * bw) * w))
    right = min(w, round((x + TORSO[2] * bw) * w))
    top = max(0, round((y + TORSO[1] * bh) * h))
    bottom = min(h, round((y + TORSO[3] * bh) * h))
    if right - left < WIDTH // 8 or bottom - top < HEIGHT // 8:
        return None
    return cv.resize(image[top:bottom, left:right], (WIDTH, HEIGHT))


def targets(number: str) -> tuple[int, int, int]:
    """Readable flag, tens class (0 = none) and units digit for one label."""
    if number == "hidden":
        return 0, 0, 0
    tens = 0 if len(number) == 1 else int(number[0]) + 1
    return 1, tens, int(number[-1])


def labelled_crops(dataset: Path) -> dict[str, list]:
    """Torso crops and targets of reviewed players, per snapshot split."""
    cv, _ = modules()
    manifest = json.loads((dataset / "manifest.json").read_text())
    crops: dict[str, list] = {"train": [], "val": [], "test": []}
    for record in manifest["frames"]:
        players = [
            obj
            for obj in record["annotation"]["objects"]
            if obj["label"] == "player" and "shirt_number" in obj
        ]
        if not players:
            continue
        image = cv.imread(str(dataset / record["image"]))
        if image is None:
            continue
        for obj in players:
            crop = torso(image, obj["bbox"])
            if crop is not None and sum(map(len, crops.values())) < MAX_CROPS:
                crops[record["split"]].append((crop, targets(obj["shirt_number"])))
    return crops


def synthetic(count: int, seed: int) -> list:
    """Render numbers on plain shirt colours, with a share of unnumbered shirts."""
    cv, np = modules()
    rng = np.random.default_rng(seed)
    output = []
    for _ in range(count):
        shirt, ink = SHIRTS[rng.integers(len(SHIRTS))]
        canvas = np.full((HEIGHT * 2, WIDTH * 2, 3), shirt, np.float32)
        canvas = np.clip(canvas + rng.normal(0, 12, canvas.shape), 0, 255).astype(
            np.uint8
        )
        number = "hidden"
        if rng.random() < NUMBERED_SHARE:
            number = str(rng.integers(100))
            scale = rng.uniform(1.6, 2.8)
            thickness = int(rng.integers(3, 8))
            font = int(FONTS[rng.integers(len(FONTS))])
            (tw, th), _ = cv.getTextSize(number, font, scale, thickness)
            origin = (
                max(0, (canvas.shape[1] - tw) // 2 + int(rng.integers(-10, 11))),
                min(
                    canvas.shape[0] - 5,
                    (canvas.shape[0] + th) // 2 + int(rng.integers(-15, 16)),
                ),
            )
            cv.putText(canvas, number, origin, font, scale, ink, thickness, cv.LINE_AA)
        turn = cv.getRotationMatrix2D(
            (canvas.shape[1] / 2, canvas.shape[0] / 2),
            rng.uniform(-12, 12),
            rng.uniform(0.75, 1.1),
        )
        canvas = cv.warpAffine(canvas, turn, canvas.shape[1::-1], borderValue=shirt)
        canvas = cv.GaussianBlur(canvas, (0, 0), rng.uniform(0.3, 2.2))
        crop = cv.resize(canvas, (WIDTH, HEIGHT), interpolation=cv.INTER_AREA)
        output.append((crop, targets(number)))
    return output


def tensor(crops: list) -> Any:  # noqa: ANN401
    """Normalize BGR uint8 crops into an NCHW float32 RGB array."""
    _, np = modules()
    images = np.stack(crops)[..., ::-1].astype(np.float32) / 255.0
    return ((images - MEAN) / STD).transpose(0, 3, 1, 2).astype(np.float32)


def network(pretrained: bool) -> Any:  # noqa: ANN401
    """MobileNet-V3 small with readable, tens and units heads."""
    torch = importlib.import_module("torch")
    vision = importlib.import_module("torchvision")
    weights = "DEFAULT" if pretrained else None
    backbone = vision.models.mobilenet_v3_small(weights=weights)
    features = backbone.classifier[0].in_features

    class Reader(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = backbone.features
            self.pool = torch.nn.AdaptiveAvgPool2d(1)
            self.readable = torch.nn.Linear(features, 2)
            self.tens = torch.nn.Linear(features, TENS)
            self.units = torch.nn.Linear(features, UNITS)

        def forward(self, x: Any) -> tuple:  # noqa: ANN401
            shared = self.pool(self.features(x)).flatten(1)
            return self.readable(shared), self.tens(shared), self.units(shared)

    return Reader()


def augment(crop: Any, rng: Any) -> Any:  # noqa: ANN401
    """Shift, scale, blur and relight; never mirror, which changes digits."""
    cv, np = modules()
    turn = cv.getRotationMatrix2D(
        (WIDTH / 2 + rng.uniform(-4, 4), HEIGHT / 2 + rng.uniform(-6, 6)),
        rng.uniform(-8, 8),
        rng.uniform(0.85, 1.15),
    )
    crop = cv.warpAffine(crop, turn, (WIDTH, HEIGHT), borderMode=cv.BORDER_REFLECT)
    if rng.random() < BLUR_SHARE:
        crop = cv.GaussianBlur(crop, (0, 0), rng.uniform(0.3, 1.5))
    gain = rng.uniform(0.7, 1.3)
    return np.clip(
        crop.astype(np.float32) * gain + rng.uniform(-25, 25), 0, 255
    ).astype(np.uint8)


@dataclass(frozen=True)
class Fit:
    """Bounded training settings; tests shrink them."""

    epochs: int = EPOCHS
    batch: int = BATCH
    pretrained: bool = True
    seed: int = 42
    device: str = "cpu"


def losses(outputs: tuple, labels: Any) -> Any:  # noqa: ANN401
    """Readable loss everywhere; digit losses only on readable shirts."""
    torch = importlib.import_module("torch")
    readable, tens, units = outputs
    loss = torch.nn.functional.cross_entropy(readable, labels[:, 0])
    mask = labels[:, 0] == 1
    if mask.any():
        loss += torch.nn.functional.cross_entropy(tens[mask], labels[mask, 1])
        loss += torch.nn.functional.cross_entropy(units[mask], labels[mask, 2])
    return loss


def fit(train: list, fit_options: Fit, stopped: Callable[[], bool] | None) -> Any:  # noqa: ANN401
    """Train the reader on crops with targets; returns the model on CPU.

    Raises:
        KeyboardInterrupt: The job was cancelled or reached its deadline.

    """
    torch = importlib.import_module("torch")
    _, np = modules()
    torch.manual_seed(fit_options.seed)
    rng = np.random.default_rng(fit_options.seed)
    device = torch_device(fit_options.device)
    model = network(fit_options.pretrained).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE)
    steps = max(1, fit_options.epochs * -(-len(train) // fit_options.batch))
    schedule = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=LEARNING_RATE, total_steps=steps
    )
    labels = np.array([t for _, t in train], dtype=np.int64)
    model.train()
    for _ in range(fit_options.epochs):
        order = rng.permutation(len(train))
        for start in range(0, len(order), fit_options.batch):
            if stopped and stopped():
                raise KeyboardInterrupt
            batch = order[start : start + fit_options.batch]
            images = tensor([augment(train[int(i)][0], rng) for i in batch])
            outputs = model(torch.from_numpy(images).to(device))
            loss = losses(outputs, torch.from_numpy(labels[batch]).to(device))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            schedule.step()
    return model.cpu().eval()


def torch_device(device: str) -> str:
    """Map the detector device option ("cpu", "0", "0,1", "mps") to one device."""
    first = device.split(",", maxsplit=1)[0].strip()
    return f"cuda:{first}" if first.isdigit() else first


def save(model: Any, target: Path) -> None:  # noqa: ANN401
    """Store only tensors, so loading the file can never execute code."""
    torch = importlib.import_module("torch")
    torch.save({"version": VERSION, "state_dict": model.state_dict()}, target)


def score(
    readings: list[tuple[str | None, float]], crops: list, threshold: float
) -> dict[str, Any]:
    """Held-out accuracy of numbers reported at one confidence threshold."""
    if not crops:
        return {"crops": 0}
    readable = [(r, t) for r, (_, t) in zip(readings, crops, strict=True) if t[0] == 1]
    confident = [(r, t) for r, t in readable if r[1] >= threshold]
    exact = sum(r[0] == number(t) for r, t in confident)
    wrong_hidden = sum(
        r[1] >= threshold
        for r, (_, t) in zip(readings, crops, strict=True)
        if t[0] == 0
    )
    reported = len(confident) + wrong_hidden
    return {
        "crops": len(crops),
        "readable": len(readable),
        "read": len(confident),
        "correct": exact,
        "threshold": threshold,
        "precision": round(exact / reported, 3) if reported else None,
        "recall": round(exact / len(readable), 3) if readable else None,
        "hidden_read_as_number": wrong_hidden,
    }


def calibrate(readings: list, crops: list) -> float | None:
    """Lowest threshold whose reported numbers reach the target precision."""
    for threshold in THRESHOLDS:
        result = score(readings, crops, threshold)
        reported = result["read"] + result["hidden_read_as_number"]
        if reported < MIN_CALIBRATION_READS:
            return None
        if result["precision"] >= TARGET_PRECISION:
            return threshold
    return None


def number(target: tuple[int, int, int]) -> str | None:
    """Return the number a target encodes, or None when unreadable."""
    readable, tens, units = target
    if not readable:
        return None
    return f"{tens - 1}{units}" if tens else str(units)


def train_reader(
    dataset: Path,
    weights: Path,
    fit_options: Fit | None = None,
    stopped: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Train and export the reader beside a detector checkpoint.

    Returns a provenance record; "skipped" when there are too few labels.
    """
    fit_options = fit_options or Fit()
    started = time.monotonic()
    crops = labelled_crops(dataset)
    readable = sum(t[0] for _, t in crops["train"])
    record: dict[str, Any] = {
        "version": VERSION,
        "labels": {split: len(items) for split, items in crops.items()},
        "readable_train_labels": readable,
    }
    if readable < MIN_READABLE_LABELS:
        return {
            **record,
            "status": "skipped",
            "reason": f"Needs {MIN_READABLE_LABELS} readable training labels",
        }
    count = max(MIN_SYNTHETIC, SYNTHETIC_PER_REAL * len(crops["train"]))
    model = fit(
        crops["train"] + synthetic(count, fit_options.seed), fit_options, stopped
    )
    target = weights / FILE
    save(model, target)
    # Calibrate with the same reading code clip runs use.
    readings = NumberReader(target).read([c for c, _ in crops["val"]])
    threshold = calibrate(readings, crops["val"])
    return {
        **record,
        # Without a calibrated threshold the reader is kept for inspection but
        # clip runs do not use it: a wrong number would join two players.
        "status": "completed" if threshold is not None else "uncalibrated",
        "file": FILE,
        "sha256": digest(target),
        "synthetic": count,
        "epochs": fit_options.epochs,
        "pretrained": fit_options.pretrained,
        "threshold": threshold,
        "target_precision": TARGET_PRECISION,
        "validation": score(readings, crops["val"], threshold or 1.0),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


class NumberReader:
    """CPU reader returning (number or None, confidence) per torso crop."""

    def __init__(self, path: Path) -> None:
        """Load one saved reader; the file holds tensors only.

        Raises:
            ValueError: The file is from an incompatible reader version.

        """
        torch = importlib.import_module("torch")
        saved = torch.load(path, map_location="cpu", weights_only=True)
        if saved.get("version") != VERSION:
            raise ValueError("Unsupported shirt-number reader version")
        self.model = network(pretrained=False)
        self.model.load_state_dict(saved["state_dict"])
        self.model.eval()
        self.sha256 = digest(path)

    def read(self, crops: list) -> list[tuple[str | None, float]]:
        """Most likely number and its joint probability for each crop."""
        if not crops:
            return []
        torch = importlib.import_module("torch")
        _, np = modules()
        with torch.inference_mode():
            outputs = self.model(torch.from_numpy(tensor(crops)))
        readable, tens, units = (softmax(v.numpy()) for v in outputs)
        output = []
        for r, t, u in zip(readable, tens, units, strict=True):
            ten, unit = int(np.argmax(t)), int(np.argmax(u))
            confidence = float(r[1] * t[ten] * u[unit])
            output.append((number((1, ten, unit)), confidence))
        return output


def softmax(values: Any) -> Any:  # noqa: ANN401
    """Row-wise softmax of logits."""
    _, np = modules()
    shifted = np.exp(values - values.max(axis=1, keepdims=True))
    return shifted / shifted.sum(axis=1, keepdims=True)


def save_record(root: Path, record: dict[str, Any]) -> None:
    """Keep the reader's provenance beside its file for clip-run receipts."""
    atomic_json(root / "numbers.json", record)


class ShirtNumbers:
    """Read each track's shirt a few times a second during a clip run.

    Only unobstructed torsos are read, so a teammate crossing in front cannot
    lend their number. Readings below the calibrated threshold are dropped.
    """

    def __init__(self, reader: NumberReader, threshold: float) -> None:
        """Use one calibrated reader for the whole run."""
        self.reader = reader
        self.threshold = threshold
        self.last: dict[str, float] = {}
        self.stats = {"read": 0, "reported": 0, "seconds": 0.0}

    def attach(self, image: Any, persons: list[dict], time_seconds: float) -> None:  # noqa: ANN401
        """Add `shirt_number` and its confidence to confidently read players."""
        clear_torso = importlib.import_module(
            ".clip_identity", __package__
        ).IdentityMemory.clear_torso
        started = time.monotonic()
        chosen, crops = [], []
        for person in persons:
            identity = person.get("track_id")
            if (
                person["label"] != "player"
                or not identity
                or person.get("identity_uncertain")
                or time_seconds - self.last.get(identity, -1e9) < READ_INTERVAL
                or not clear_torso(person, persons)
            ):
                continue
            crop = torso(image, person["observed_bbox"])
            if crop is not None:
                chosen.append(person)
                crops.append(crop)
        for person, (value, confidence) in zip(
            chosen, self.reader.read(crops), strict=True
        ):
            self.last[person["track_id"]] = time_seconds
            self.stats["read"] += 1
            if value is not None and confidence >= self.threshold:
                self.stats["reported"] += 1
                person["shirt_number"] = value
                person["shirt_number_confidence"] = round(confidence, 3)
        self.stats["seconds"] += time.monotonic() - started

    def snapshot(self) -> dict[str, Any]:
        """Receipt counts for the run manifest."""
        return {
            "reader_sha256": self.reader.sha256,
            "threshold": self.threshold,
            **{k: round(v, 3) for k, v in self.stats.items()},
        }


def beside(weights: str) -> tuple[ShirtNumbers | None, dict[str, Any]]:
    """Find a calibrated reader trained with a detector checkpoint.

    Checkpoints live at `runs/<id>/fit/weights/best.pt`; the training run's
    manifest records whether its reader reached the precision target.
    """
    path = Path(weights)
    reader_path = path.parent / FILE
    manifest = path.parent.parent.parent / "run.json"
    if not reader_path.is_file() or not manifest.is_file():
        return None, {"status": "unavailable"}
    record = json.loads(manifest.read_text()).get("numbers", {})
    if record.get("status") != "completed" or record.get("threshold") is None:
        return None, {"status": record.get("status", "unavailable")}
    if digest(reader_path) != record.get("sha256"):
        return None, {"status": "checksum_mismatch"}
    reader = NumberReader(reader_path)
    return ShirtNumbers(reader, float(record["threshold"])), {"status": "enabled"}
