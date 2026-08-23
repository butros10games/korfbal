"""Request-local control for coalescing live revision signals."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_signals_suppressed = ContextVar("korfbal_live_signals_suppressed", default=False)
_tracker_delete_side_effects_suppressed = ContextVar(
    "korfbal_tracker_delete_side_effects_suppressed",
    default=False,
)


def live_update_signals_suppressed() -> bool:
    """Return whether a command is coalescing its model-level revisions."""
    return _signals_suppressed.get()


def tracker_delete_side_effects_suppressed() -> bool:
    """Return whether tracker signals are suppressed for a deletion cascade."""
    return _tracker_delete_side_effects_suppressed.get()


@contextmanager
def suppress_live_update_signals() -> Iterator[None]:
    """Suppress per-model revisions while a command records one revision."""
    token = _signals_suppressed.set(True)
    try:
        yield
    finally:
        _signals_suppressed.reset(token)


@contextmanager
def suppress_tracker_delete_side_effects() -> Iterator[None]:
    """Suppress tracker signal side effects while deleting an owning match."""
    token = _tracker_delete_side_effects_suppressed.set(True)
    try:
        yield
    finally:
        _tracker_delete_side_effects_suppressed.reset(token)
