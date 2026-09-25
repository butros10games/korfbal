"""Run a CPU detector in its isolated Python environment from an immutable input."""

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .store import Store
from .training import RunOptions, proposals


class ProjectionStore(Store):
    """Read a database projection while writing only independent model artifacts."""

    def __init__(self, root: Path, input_path: Path) -> None:
        """Bind server-created input and registered media paths."""
        self.root = root.resolve()
        self.path = input_path
        self.lock = threading.RLock()

    @contextmanager
    def video_source(self, relative: str) -> Iterator[str]:
        """Use only the loopback reader supplied by the trusted parent worker.

        Yields:
            A seekable source kept alive by the parent subprocess owner.

        """
        payload = json.loads(self.path.read_text())
        source = payload.get("video_source")
        if source and payload.get("match", {}).get("video") == relative:
            parsed = urlsplit(source)
            if (
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and not parsed.username
                and not parsed.password
            ):
                yield source
                return
        with super().video_source(relative) as source:
            yield source


def main() -> None:
    """Execute one bounded proposal job; never change human annotation rows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("input", type=Path)
    parser.add_argument("match")
    parser.add_argument("weights")
    args = parser.parse_args()
    proposals(
        ProjectionStore(args.root, args.input),
        args.weights,
        options=RunOptions(match_id=args.match, limit=25),
    )


if __name__ == "__main__":
    main()
