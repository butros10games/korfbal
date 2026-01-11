"""Environment helpers for settings modules."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv


BASE_DIR: Final[Path] = Path(__file__).resolve().parents[2]
PROJECT_DIR: Final[Path] = BASE_DIR / "korfbal"

_env_file = BASE_DIR / ".env"
if _env_file.exists():
    load_dotenv(_env_file)


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """Return an env var value.

    Raises:
        RuntimeError: When `required=True` and the variable is missing/empty.

    """
    value = os.getenv(name, default)
    if required and value in {None, ""}:
        raise RuntimeError(f"Environment variable '{name}' is required")
    return "" if value is None else value


def env_bool(name: str, default: bool = False) -> bool:
    """Return a bool env var.

    Truthy values: `1,true,yes,on` (case-insensitive).
    """
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    """Return an int env var, falling back to `default`."""
    raw = os.getenv(name)
    return int(raw) if raw else default


def env_list(name: str, default: str = "", sep: str = ",") -> list[str]:
    """Return a list env var (split + trimmed)."""
    raw = os.getenv(name, default) or ""
    return [part.strip() for part in raw.split(sep) if part.strip()]


def sorted_hosts(values: list[str]) -> list[str]:
    """Return a deterministic list of non-empty hosts."""
    return sorted({host for host in values if host})
