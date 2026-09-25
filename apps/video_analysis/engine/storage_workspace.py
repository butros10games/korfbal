"""Cross-process ownership of disposable video and training working files."""

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
from pathlib import Path
import shutil


STORAGE_PROTOCOL = 2


@contextmanager
def storage_lease(root: Path, *, name: str = ".storage.lock") -> Iterator[None]:
    """Keep eviction outside worker/controller use; kernel releases on process death.

    Yields:
        Exclusive ownership of this workspace's materialized bulk data.

    """
    root.mkdir(parents=True, exist_ok=True)
    with (root / name).open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def reserve_space(root: Path, additional: int, maximum: int, headroom: int) -> None:
    """Check aggregate working bytes and filesystem headroom before allocation.

    Raises:
        ValueError: The operation cannot fit within the configured staging budget.

    """
    used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    if (
        used + additional > maximum
        or shutil.disk_usage(root).free < additional + headroom
    ):
        raise ValueError("Video workspace staging budget exceeded")


def clear_incomplete_cache(root: Path) -> None:
    """Remove interrupted downloads while excluding active API cache writers."""
    with storage_lease(root, name=".cache.lock"):
        for path in root.rglob(".cache-*"):
            if path.is_file():
                path.unlink()
