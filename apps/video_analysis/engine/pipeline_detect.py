"""Isolated bounded draft inference with immutable input and output artifacts."""

import argparse
import json
from pathlib import Path

from .store import atomic_json
from .training import RunOptions, detector, predict_with_options
from .vision import digest


def main() -> None:
    """Infer every requested frame; results remain proposals, never approvals."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    model = detector(data["weights"])
    options = RunOptions(device="cpu", limit=25)
    records = []
    for frame in data["frames"]:
        prediction, classes = predict_with_options(model, Path(frame["path"]), options)
        records.append({
            **{key: value for key, value in frame.items() if key != "path"},
            "image_sha256": digest(Path(frame["path"])),
            "prediction": prediction,
            "classes": classes,
        })
    atomic_json(
        args.output,
        {
            "model": data["model"],
            "weights_sha256": digest(Path(data["weights"])),
            "config": {"imgsz": options.imgsz, "confidence": options.confidence},
            "frames": records,
        },
    )


if __name__ == "__main__":
    main()
