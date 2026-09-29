"""Architecture boundary regression tests."""

from __future__ import annotations

import ast
from pathlib import Path


APPS_DIR = Path(__file__).resolve().parents[2]
BOUNDARY_FILE_NAMES = {"signals.py", "tasks.py"}
BOUNDARY_DIR_NAMES = {"application", "domain", "services", "signals", "tasks"}
# Pure layers receive capabilities; only outer entrypoints (API, tasks, signals,
# management commands) may resolve production implementations.
CAPABILITY_CONSUMER_DIR_NAMES = {"application", "domain", "queries", "services"}
CAPABILITY_CONSUMER_FILE_NAMES = {"queries.py"}
FORBIDDEN_FRAMEWORK_PREFIXES = ("django.http", "rest_framework")
FORBIDDEN_LOCAL_LAYERS = {"adapters", "api"}
COMPOSITION_LAYER = "composition"
DYNAMIC_IMPORTERS = {"import_module", "__import__"}
APP_LAYER_DEPTH = 3  # apps.<app>.<layer>


def _is_boundary_file(path: Path) -> bool:
    relative_parts = path.relative_to(APPS_DIR).parts
    return bool(BOUNDARY_DIR_NAMES.intersection(relative_parts)) or (
        path.name in BOUNDARY_FILE_NAMES
    )


def _is_capability_consumer(path: Path) -> bool:
    relative_parts = path.relative_to(APPS_DIR).parts
    return bool(CAPABILITY_CONSUMER_DIR_NAMES.intersection(relative_parts)) or (
        path.name in CAPABILITY_CONSUMER_FILE_NAMES
    )


def _module_name(path: Path) -> str:
    parts = ["apps", *path.relative_to(APPS_DIR).with_suffix("").parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _resolve(module: str, level: int, *, importer: str, is_package: bool) -> str:
    """Return the absolute module name of a possibly relative import."""
    if level == 0:
        return module
    package = importer.split(".") if is_package else importer.split(".")[:-1]
    base = package[: len(package) - (level - 1)]
    return ".".join([*base, module] if module else base)


def _dynamic_import(node: ast.AST) -> str | None:
    """Return the literal module of ``import_module("...")`` style calls."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    function = node.func
    name = function.id if isinstance(function, ast.Name) else None
    if isinstance(function, ast.Attribute):
        name = function.attr
    argument = node.args[0]
    if name not in DYNAMIC_IMPORTERS or not isinstance(argument, ast.Constant):
        return None
    return argument.value if isinstance(argument.value, str) else None


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
        elif (dynamic := _dynamic_import(node)) is not None:
            modules.append((node.lineno, dynamic, 0))
    return modules


def _app_layer(module: str) -> str | None:
    """Return the top-level layer of an ``apps.<app>.<layer>`` module."""
    parts = module.split(".")
    if len(parts) < APP_LAYER_DEPTH or parts[0] != "apps":
        return None
    return parts[APP_LAYER_DEPTH - 1]


def _is_forbidden_import(*, module: str, relative_level: int) -> bool:
    if any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_FRAMEWORK_PREFIXES
    ):
        return True

    module_parts = module.split(".")
    is_local_import = relative_level > 0 or module.startswith("apps.")
    return is_local_import and bool(FORBIDDEN_LOCAL_LAYERS.intersection(module_parts))


def _iter_sources() -> list[tuple[Path, str, list[tuple[int, str]]]]:
    sources = []
    for path in sorted(APPS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        importer = _module_name(path)
        tree = ast.parse(path.read_text(), filename=str(path))
        imports = [
            (
                line_number,
                _resolve(
                    module,
                    level,
                    importer=importer,
                    is_package=path.name == "__init__.py",
                ),
            )
            for line_number, module, level in _imported_modules(tree)
        ]
        sources.append((path, importer, imports))
    return sources


def test_layer_import_boundaries_are_enforced() -> None:
    """Inner and inbound layers must keep their dependency direction."""
    inner_violations: list[str] = []
    api_violations: list[str] = []

    for path, _importer, imports in _iter_sources():
        relative_path = path.relative_to(APPS_DIR)
        is_inner_boundary = _is_boundary_file(path)
        is_api_boundary = "api" in relative_path.parts
        for line_number, module in imports:
            location = f"{relative_path}:{line_number} imports {module}"
            if is_inner_boundary and (
                _is_forbidden_import(module=module, relative_level=0)
                or _app_layer(module) in FORBIDDEN_LOCAL_LAYERS
            ):
                inner_violations.append(location)
            if is_api_boundary and _app_layer(module) == "adapters":
                api_violations.append(location)

    assert inner_violations == []
    assert api_violations == []


def test_capability_consumers_never_resolve_composition_roots() -> None:
    """Services receive capabilities; only outer entrypoints read composition."""
    violations = [
        f"{path.relative_to(APPS_DIR)}:{line_number} imports {module}"
        for path, _importer, imports in _iter_sources()
        if _is_capability_consumer(path)
        for line_number, module in imports
        if _app_layer(module) == COMPOSITION_LAYER
    ]

    assert violations == []


def test_boundary_file_detection_includes_task_and_signal_packages() -> None:
    """Nested task/signal modules are application boundaries too."""
    assert _is_boundary_file(APPS_DIR / "player" / "tasks" / "downloads.py")
    assert _is_boundary_file(APPS_DIR / "player" / "signals" / "players.py")
    assert _is_capability_consumer(APPS_DIR / "video_analysis" / "queries.py")
    assert not _is_capability_consumer(APPS_DIR / "video_analysis" / "tasks.py")


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


def test_relative_and_dynamic_imports_resolve_to_app_layers() -> None:
    """Relative and string imports cannot hide a composition or adapter dependency."""
    service = "apps.video_analysis.services.clips"
    assert (
        _resolve("composition", 2, importer=service, is_package=False)
        == "apps.video_analysis.composition"
    )
    assert _resolve("", 2, importer=service, is_package=False) == "apps.video_analysis"
    engine = "apps.video_analysis.engine.remote.controller"
    engine_adapter = _resolve("adapters", 1, importer=engine, is_package=False)
    assert _app_layer(engine_adapter) == "engine"
    tree = ast.parse(
        "from importlib import import_module\n"
        "import_module('apps.competition.composition')\n"
        "__import__('apps.player.adapters.outbound')\n"
    )
    dynamic = [module for _, module, _ in _imported_modules(tree)]
    assert "apps.competition.composition" in dynamic
    assert "apps.player.adapters.outbound" in dynamic
