"""Regression checks for the incremental ports-and-adapters boundaries."""

from __future__ import annotations

import ast
from pathlib import Path


APPS_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_DIRECTORIES = (APPS_ROOT / "game_tracker" / "domain",)
PORT_FILES = (
    APPS_ROOT / "game_tracker" / "application" / "ports.py",
    APPS_ROOT / "player" / "application" / "ports.py",
)
FORBIDDEN_DOMAIN_IMPORTS = (
    "celery",
    "django",
    "kombu",
    "rest_framework",
    "apps.game_tracker.adapters",
    "apps.game_tracker.models",
    "apps.game_tracker.services",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_domain_rules_do_not_depend_on_framework_or_adapters() -> None:
    """Domain packages remain directly testable without Django infrastructure."""
    violations: list[str] = []
    for directory in DOMAIN_DIRECTORIES:
        for path in directory.glob("*.py"):
            for imported in _imports(path):
                if imported.startswith(FORBIDDEN_DOMAIN_IMPORTS):
                    violations.append(f"{path.relative_to(APPS_ROOT)} -> {imported}")

    assert violations == []


def test_outbound_ports_do_not_import_implementations() -> None:
    """Port definitions remain independent from Django and adapter packages."""
    forbidden = ("django", "celery", "kombu", "requests", "apps.player.adapters")
    violations = [
        f"{path.relative_to(APPS_ROOT)} -> {imported}"
        for path in PORT_FILES
        for imported in _imports(path)
        if imported.startswith(forbidden)
    ]

    assert violations == []
