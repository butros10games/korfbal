"""The vision engine runs unchanged inside Django, the CLI alias and GPU kits."""

from __future__ import annotations

import ast
from pathlib import Path
import sys

import pytest


ENGINE = Path(__file__).resolve().parents[1] / "engine"
CLI_ALIAS = Path(__file__).resolve().parents[6] / "scripts/python/korfbal_review"
HOST_PREFIXES = ("apps", "django", "korfbal", "rest_framework", "scripts")


def _engine_modules() -> list[Path]:
    return [path for path in ENGINE.rglob("*.py") if "__pycache__" not in path.parts]


def _is_host(module: str) -> bool:
    return module.split(".", maxsplit=1)[0] in HOST_PREFIXES


def _dynamic_import(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not node.args:
        return None
    name = getattr(node.func, "attr", getattr(node.func, "id", ""))
    target = node.args[0]
    if name not in {"import_module", "__import__"} or not isinstance(
        target, ast.Constant
    ):
        return None
    return target.value if isinstance(target.value, str) else None


def _import_targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return ["." * node.level + (node.module or "")]
    target = _dynamic_import(node)
    return [target] if target is not None else []


def _violations(path: Path) -> list[str]:
    depth = len(path.relative_to(ENGINE).parts)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
            continue
        for target in _import_targets(node):
            if _is_host(target):
                found.append(f"{node.lineno}: host import {target}")
            elif len(target) - len(target.lstrip(".")) > depth:
                found.append(f"{node.lineno}: relative import leaves the engine")
    return [f"{path.relative_to(ENGINE)}:{line}" for line in found]


def test_engine_only_imports_itself_relatively() -> None:
    """Absolute host imports load a second engine copy under the CLI alias."""
    violations = [line for path in _engine_modules() for line in _violations(path)]

    assert violations == []


def test_cli_alias_does_not_extend_the_import_path() -> None:
    """The alias must not make the Django project importable from the CLI."""
    for path in CLI_ALIAS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            target for node in ast.walk(tree) for target in _import_targets(node)
        }
        assert "sys" not in imported, path.name


def test_scripts_load_the_engine_through_the_cli_alias() -> None:
    """Importing both paths gives two engine copies whose patches and types diverge."""
    engine_paths = (
        "apps.video_analysis.engine",
        "apps.django_projects.korfbal.apps.video_analysis.engine",
    )
    violations = [
        f"{path.name}:{node.lineno}"
        for path in CLI_ALIAS.parent.rglob("*.py")
        if "__pycache__" not in path.parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Call))
        and any(target.startswith(engine_paths) for target in _import_targets(node))
    ]

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import apps.video_analysis.engine.store",
        "from apps.video_analysis.engine.store import Store",
        "importlib.import_module('apps.video_analysis.engine.store')",
        "__import__('apps.video_analysis.engine.store')",
        "from .. import store",
        "importlib.import_module('..store', __package__)",
    ],
)
def test_engine_guard_rejects_all_host_import_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """Alternate import syntax cannot bypass the location-independent boundary."""
    monkeypatch.setattr(sys.modules[__name__], "ENGINE", tmp_path)
    path = tmp_path / "example.py"
    path.write_text(source)

    assert len(_violations(path)) == 1


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("import sys", "sys"),
        ("from sys import path", "sys"),
        ("importlib.import_module('sys')", "sys"),
        ("__import__('sys')", "sys"),
    ],
)
def test_alias_guard_recognizes_all_import_forms(source: str, target: str) -> None:
    """The alias must not regain sys.path access through a different import form."""
    imported = [
        module
        for node in ast.walk(ast.parse(source))
        for module in _import_targets(node)
    ]
    assert imported == [target]
