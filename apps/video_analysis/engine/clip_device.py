"""Opt-in GPU execution for the clip worker; the CPU stays the default.

Production clip workers are CPU-only. A worker started with
``KORFBAL_CLIP_DEVICE=cuda`` runs the same ONNX exports (detector, appearance
and shirt-number models) through ONNX Runtime's CUDA provider instead, with the
same preprocessing and decoding. A worker that asks for CUDA but cannot get it
fails rather than silently running twenty times slower on the CPU.
"""

from __future__ import annotations

import importlib
import os
from typing import Any


DEVICE_ENV = "KORFBAL_CLIP_DEVICE"
DEVICES = ("cpu", "cuda")
CPU = "CPUExecutionProvider"
CUDA = "CUDAExecutionProvider"


def package_version(name: str) -> str:
    """Installed version of a package; ONNX Runtime may be its GPU build."""
    metadata = importlib.import_module("importlib.metadata")
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        if name != "onnxruntime":
            raise
        return metadata.version("onnxruntime-gpu")


def device() -> str:
    """Return the configured clip device.

    Raises:
        ValueError: The setting names an unsupported device.

    """
    value = os.environ.get(DEVICE_ENV, "cpu")
    if value not in DEVICES:
        raise ValueError(f"{DEVICE_ENV} must be cpu or cuda")
    return value


def providers() -> list[Any]:
    """ONNX Runtime providers for the configured device, CPU last as fallback.

    Raises:
        ValueError: CUDA was requested but this runtime cannot provide it.

    """
    if device() == "cpu":
        return [CPU]
    runtime = importlib.import_module("onnxruntime")
    # Pip-installed CUDA/cuDNN wheels are found only after an explicit preload.
    preload = getattr(runtime, "preload_dlls", None)
    if preload is not None:
        preload()
    if CUDA not in runtime.get_available_providers():
        raise ValueError("CUDA was requested but ONNX Runtime has no CUDA provider")
    # FP32 everywhere, as on the CPU: TF32 would change detections slightly.
    return [(CUDA, {"use_tf32": "0", "arena_extend_strategy": "kSameAsRequested"}), CPU]


def session(path: object, threads: int, *, spin: bool = True) -> Any:  # noqa: ANN401
    """Open one bounded ONNX session on the configured device.

    ``spin=False`` keeps idle intra-op threads from busy-waiting (detector).

    Raises:
        ValueError: CUDA was requested but the session fell back to the CPU.

    """
    runtime = importlib.import_module("onnxruntime")
    options = runtime.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    if not spin:
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    chosen = providers()
    opened = runtime.InferenceSession(str(path), sess_options=options, providers=chosen)
    if device() == "cuda" and opened.get_providers()[0] != CUDA:
        raise ValueError("CUDA was requested but the model runs on the CPU")
    return opened


def used(opened: Any) -> str:  # noqa: ANN401 - lazy onnxruntime session
    """Name the device a session actually runs on, for receipts."""
    names = getattr(opened, "get_providers", lambda: [CPU])()
    return "cuda" if names and names[0] == CUDA else "cpu"
