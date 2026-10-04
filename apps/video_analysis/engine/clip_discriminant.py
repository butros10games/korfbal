"""The per-clip discriminant appearance space, computed on the GPU when opted in.

The linker and the roster classifier fit the same space: tracklets are the
classes, the within-class scatter is shrunk towards its mean variance and
whitened, and the leading directions between class centres are kept. With
4,608-dimensional DINOv2-g descriptors this is the slowest step of a clip's
finish on the CPU (two 4,608 x 4,608 eigen/singular decompositions per linker
round and one scatter product per tracklet). A CUDA worker runs exactly the
same float64 operations with PyTorch on the GPU; the CPU path stays NumPy.
The results agree to rounding: eigenvector signs and rotations inside repeated
eigenvalues may differ, which leaves the whitening and every cosine unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import TYPE_CHECKING, Any

from . import clip_device


if TYPE_CHECKING:
    from numpy.typing import NDArray


@dataclass(frozen=True)
class Fit:
    """How many directions to keep, the scatter shrinkage and the eigenvalue floor."""

    dimensions: int
    shrinkage: float
    epsilon: float


# Room a 4,608-dimensional fit needs on the card (scatter, eigen workspace,
# centres). Several section workers share one GPU; with less free memory the
# same fit runs on the CPU instead of failing the section.
GPU_HEADROOM = 2 << 30


def gpu() -> bool:
    """Whether this worker fits appearance spaces on the GPU."""
    return clip_device.device() == "cuda"


def room() -> bool:
    """Whether the GPU has room for one fit right now."""
    torch = importlib.import_module("torch")
    # Return this worker's cached blocks first (descriptor batches leave some).
    torch.cuda.empty_cache()
    free, _ = torch.cuda.mem_get_info()
    return free >= GPU_HEADROOM


def out_of_memory(error: BaseException) -> bool:
    """Recognise a CUDA allocation failure (PyTorch's own type or its message)."""
    torch = importlib.import_module("torch")
    kind = getattr(torch, "OutOfMemoryError", None)
    return (kind is not None and isinstance(error, kind)) or "out of memory" in str(
        error
    )


def transform(
    values: NDArray[Any],
    labels: NDArray[Any],
    classes: list[int],
    fit: Fit,
) -> NDArray[Any]:
    """Return ``whiten @ axes[:dimensions].T`` for centred float64 ``values``.

    Same steps as the NumPy fits in ``clip_linking`` and ``clip_match_evidence``;
    on the GPU when it has room, else (or after an allocation failure) on the CPU.

    Raises:
        RuntimeError: The GPU failed for another reason than memory.

    """
    if not room():
        return cpu_transform(values, labels, classes, fit)
    try:
        return gpu_transform(values, labels, classes, fit)
    except RuntimeError as error:
        if not out_of_memory(error):
            raise
        importlib.import_module("torch").cuda.empty_cache()
        return cpu_transform(values, labels, classes, fit)


def cpu_transform(
    values: NDArray[Any], labels: NDArray[Any], classes: list[int], fit: Fit
) -> NDArray[Any]:
    """Fit on the CPU: per-class scatter, then ``numpy_space``."""
    np = importlib.import_module("numpy")
    within = np.zeros((values.shape[1],) * 2)
    centres = []
    for label in classes:
        members = values[labels == label]
        centre = members.mean(axis=0)
        centres.append(centre)
        within += (members - centre).T @ (members - centre)
    within /= sum(int((labels == label).sum()) for label in classes)
    return numpy_space(within, np.array(centres), fit)


def gpu_transform(
    values: NDArray[Any], labels: NDArray[Any], classes: list[int], fit: Fit
) -> NDArray[Any]:
    """Fit with PyTorch float64 on the GPU."""
    np = importlib.import_module("numpy")
    torch = importlib.import_module("torch")
    device = torch.device("cuda")
    # Classes move to the GPU one at a time: several workers share one card.
    order = np.argsort(labels, kind="stable")
    sorted_labels = labels[order]
    within = torch.zeros(
        (values.shape[1], values.shape[1]), dtype=torch.float64, device=device
    )
    centres = []
    total = 0
    for label in classes:
        low, high = np.searchsorted(sorted_labels, [label, label + 1])
        members = torch.from_numpy(values[order[low:high]]).to(device)
        centre = members.mean(dim=0)
        centres.append(centre)
        difference = members - centre
        within += difference.T @ difference
        total += high - low
    result = finish_torch(within / total, torch.stack(centres), fit)
    del within, centres
    torch.cuda.empty_cache()
    return result


def from_scatter(within: NDArray[Any], centres: NDArray[Any], fit: Fit) -> NDArray[Any]:
    """Finish a fit from an averaged within-class scatter and class centres.

    Raises:
        RuntimeError: The GPU failed for another reason than memory.

    """
    if gpu() and room():
        torch = importlib.import_module("torch")
        try:
            return finish_torch(
                torch.from_numpy(within).to("cuda", dtype=torch.float64),
                torch.from_numpy(centres).to("cuda", dtype=torch.float64),
                fit,
            )
        except RuntimeError as error:
            if not out_of_memory(error):
                raise
            torch.cuda.empty_cache()
    return numpy_space(within, centres, fit)


def numpy_space(within: NDArray[Any], centres: NDArray[Any], fit: Fit) -> NDArray[Any]:
    """Shrink, whiten and keep the leading between-class directions (NumPy)."""
    np = importlib.import_module("numpy")
    within = (1 - fit.shrinkage) * within + fit.shrinkage * np.trace(within) / len(
        within
    ) * np.eye(len(within))
    weights, vectors = np.linalg.eigh(within)
    whiten = vectors @ np.diag(np.maximum(weights, fit.epsilon) ** -0.5) @ vectors.T
    between = centres @ whiten
    _, _, axes = np.linalg.svd(between - between.mean(axis=0), full_matrices=False)
    return whiten @ axes[: fit.dimensions].T


def finish_torch(
    within: Any,  # noqa: ANN401 - lazy torch tensor
    centres: Any,  # noqa: ANN401 - lazy torch tensor
    fit: Fit,
) -> NDArray[Any]:
    """Shrink, whiten and keep the leading between-class directions (torch)."""
    torch = importlib.import_module("torch")
    identity = torch.eye(len(within), dtype=torch.float64, device=within.device)
    within = (1 - fit.shrinkage) * within + fit.shrinkage * torch.trace(within) / len(
        within
    ) * identity
    weights, vectors = torch.linalg.eigh(within)
    whiten = (vectors * torch.clamp(weights, min=fit.epsilon) ** -0.5) @ vectors.T
    between = centres @ whiten
    _, _, axes = torch.linalg.svd(between - between.mean(dim=0), full_matrices=False)
    return (whiten @ axes[: fit.dimensions].T).cpu().numpy()
