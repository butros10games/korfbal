"""Architecture boundary regression tests."""

from __future__ import annotations

import ast
from pathlib import Path


APPS_DIR = Path(__file__).resolve().parents[2]
BOUNDARY_FILE_NAMES = {"signals.py", "tasks.py"}


def _is_boundary_file(path: Path) -> bool:
    relative_parts = path.relative_to(APPS_DIR).parts
    return "services" in relative_parts or path.name in BOUNDARY_FILE_NAMES


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    modules: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append((node.lineno, node.module))
    return modules


def test_services_tasks_and_signals_do_not_import_api_adapters() -> None:
    """Services/tasks/signals must not depend on HTTP adapter modules."""
    violations: list[str] = []

    for path in APPS_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        if not _is_boundary_file(path):
            continue

        tree = ast.parse(path.read_text(), filename=str(path))
        for line_number, module in _imported_modules(tree):
            module_parts = module.split(".")
            if module.startswith("apps.") and "api" in module_parts[2:]:
                rel_path = path.relative_to(APPS_DIR)
                violations.append(f"{rel_path}:{line_number} imports {module}")

    assert violations == []
