"""Short local training benchmarks with persisted timing and memory diagnostics."""

from __future__ import annotations

import csv
import importlib
import math
from pathlib import Path
import statistics
import time
from typing import Any

from . import training
from .store import Store, atomic_json
from .vision import artifact, start_run


MIN_BENCHMARK_EPOCHS = 2
MAX_BENCHMARK_EPOCHS = 5


def epoch_timings(path: Path) -> dict[str, Any]:
    """Summarize cumulative trainer timings, excluding epoch one from the median.

    Raises:
        ValueError: If epoch numbers or elapsed times are missing or inconsistent.

    """
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    durations = []
    previous = 0.0
    for number, row in enumerate(rows, 1):
        current = float(row["time"])
        if (
            int(row["epoch"]) != number
            or not math.isfinite(current)
            or current <= previous
        ):
            raise ValueError("Invalid benchmark epoch timing sequence")
        durations.append(current - previous)
        previous = current
    if len(durations) < MIN_BENCHMARK_EPOCHS:
        raise ValueError("Benchmark needs at least two completed epochs")
    return {
        "epoch_seconds": durations,
        "post_first_epoch_median_seconds": statistics.median(durations[1:]),
        "timed_epochs": len(durations) - 1,
        "scope": "Epoch loop including validation, excluding setup and finalization",
    }


def benchmark(
    store: Store,
    snapshot: str,
    weights: str,
    options: training.RunOptions | None = None,
) -> dict[str, Any]:
    """Run a bounded pilot on one device; retain its model and benchmark report.

    Raises:
        ValueError: If benchmark settings are invalid.
        KeyboardInterrupt: If the pilot is interrupted.

    """
    options = options or training.RunOptions(epochs=2)
    if not MIN_BENCHMARK_EPOCHS <= options.epochs <= MAX_BENCHMARK_EPOCHS:
        raise ValueError("Use 2-5 epochs for a benchmark")
    if options.device not in {"cpu", "0"}:
        raise ValueError(
            "Use cpu or 0; select other GPUs with CUDA_VISIBLE_DEVICES at launch"
        )
    if not 1 <= options.batch <= training.MAX_BATCH:
        raise ValueError("Benchmark requires a fixed batch size between 1 and 64")
    training.validate_options(options)
    root, report = start_run(
        store, "benchmark", snapshot=snapshot, weights=weights, config=vars(options)
    )
    started = time.monotonic()
    try:
        torch = importlib.import_module("torch")
        device = None if options.device == "cpu" else int(options.device)
        if device is not None:
            torch.cuda.synchronize(device)
            torch.cuda.reset_peak_memory_stats(device)
        run = training.train(store, snapshot, weights, options)
        report.update(
            train_run=run["id"],
            dataset_sha256=run["dataset_sha256"],
            environment=run["environment"],
            weights_sha256=run["weights_sha256"],
        )
        report["timing"] = epoch_timings(
            artifact(store, "runs", run["id"]) / "fit/results.csv"
        )
        report["cuda_memory"] = None
        if device is not None:
            torch.cuda.synchronize(device)
            report["cuda_memory"] = {
                "device": device,
                "name": torch.cuda.get_device_name(device),
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(device),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(device),
                "scope": "This process's PyTorch allocator, not total GPU usage",
            }
        report.update(
            status="completed",
            note=(
                "This pilot trains real weights; it does not promote a model. "
                "Compare the same snapshot, weights and settings across machines. "
                "Short runs do not predict final accuracy or guarantee long-run speed."
            ),
        )
    except (Exception, KeyboardInterrupt) as error:
        report.update(
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            error=f"{type(error).__name__}: {error}",
        )
        raise
    finally:
        report["wall_seconds"] = round(time.monotonic() - started, 3)
        atomic_json(root / "run.json", report)
    return report
