"""Architecture boundaries for tournament mode."""

import ast
from pathlib import Path


TOURNAMENT_ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_application_services_do_not_import_transport_adapters() -> None:
    """Tournament services depend on capabilities, not Channels or API code."""
    forbidden = (
        "channels",
        "rest_framework",
        "apps.tournament.adapters",
        "apps.tournament.api",
    )
    violations = [
        f"{path.relative_to(TOURNAMENT_ROOT)} -> {module}"
        for path in (TOURNAMENT_ROOT / "services").glob("*.py")
        for module in _imports(path)
        if module.startswith(forbidden)
    ]

    assert violations == []
