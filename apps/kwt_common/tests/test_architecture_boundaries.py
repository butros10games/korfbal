"""Architecture boundary regression tests."""

from __future__ import annotations

import ast
from pathlib import Path


APPS_DIR = Path(__file__).resolve().parents[2]
BOUNDARY_FILE_NAMES = {"signals.py", "tasks.py"}
BOUNDARY_DIR_NAMES = {"application", "domain", "services", "signals", "tasks"}
FORBIDDEN_FRAMEWORK_PREFIXES = ("django.http", "rest_framework")
FORBIDDEN_LOCAL_LAYERS = {"adapters", "api"}


def _is_boundary_file(path: Path) -> bool:
    relative_parts = path.relative_to(APPS_DIR).parts
    return bool(BOUNDARY_DIR_NAMES.intersection(relative_parts)) or (
        path.name in BOUNDARY_FILE_NAMES
    )


def _imported_modules(tree: ast.AST) -> list[tuple[int, str, int]]:
    modules: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base_module = node.module or ""
            modules.extend(
                (
                    node.lineno,
                    ".".join(part for part in (base_module, alias.name) if part),
                    node.level,
                )
                for alias in node.names
            )
    return modules


def _is_forbidden_import(*, module: str, relative_level: int) -> bool:
    if any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_FRAMEWORK_PREFIXES
    ):
        return True

    module_parts = module.split(".")
    is_local_import = relative_level > 0 or module.startswith("apps.")
    return is_local_import and bool(FORBIDDEN_LOCAL_LAYERS.intersection(module_parts))


def test_layer_import_boundaries_are_enforced() -> None:
    """Inner and inbound layers must keep their dependency direction."""
    inner_violations: list[str] = []
    api_violations: list[str] = []

    for path in APPS_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        relative_path = path.relative_to(APPS_DIR)
        is_inner_boundary = _is_boundary_file(path)
        is_api_boundary = "api" in relative_path.parts
        if not is_inner_boundary and not is_api_boundary:
            continue

        tree = ast.parse(path.read_text(), filename=str(path))
        for line_number, module, relative_level in _imported_modules(tree):
            location = f"{relative_path}:{line_number} imports {module}"
            if is_inner_boundary and _is_forbidden_import(
                module=module,
                relative_level=relative_level,
            ):
                inner_violations.append(location)

            module_parts = module.split(".")
            is_local_import = relative_level > 0 or module.startswith("apps.")
            if is_api_boundary and is_local_import and "adapters" in module_parts:
                api_violations.append(location)

    assert sorted(inner_violations) == []
    assert sorted(api_violations) == []


def test_boundary_file_detection_includes_task_and_signal_packages() -> None:
    """Nested task/signal modules are application boundaries too."""
    assert _is_boundary_file(APPS_DIR / "player" / "tasks" / "downloads.py")
    assert _is_boundary_file(APPS_DIR / "player" / "signals" / "players.py")


def test_forbidden_import_detection_handles_relative_and_framework_imports() -> None:
    """Relative adapters and HTTP framework imports cannot bypass the guardrail."""
    assert _is_forbidden_import(module="api.serializers", relative_level=2)
    assert _is_forbidden_import(module="adapters.outbound", relative_level=1)
    assert _is_forbidden_import(module="adapters", relative_level=1)
    assert _is_forbidden_import(module="apps.team.api.views", relative_level=0)
    assert _is_forbidden_import(module="rest_framework.response", relative_level=0)
    assert _is_forbidden_import(module="django.http", relative_level=0)
    assert not _is_forbidden_import(module="rest_frameworkish", relative_level=0)
    assert not _is_forbidden_import(module="apps.team.models", relative_level=0)
