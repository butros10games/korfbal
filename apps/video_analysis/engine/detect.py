"""Run a CPU detector in its isolated Python environment from an immutable input."""

import argparse
from pathlib import Path
import threading

from .store import Store
from .training import RunOptions, proposals


class ProjectionStore(Store):
    """Read a database projection while writing only independent model artifacts."""

    def __init__(self, root: Path, input_path: Path) -> None:
        """Bind server-created input and registered media paths."""
        self.root = root.resolve()
        self.path = input_path
        self.lock = threading.RLock()


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
