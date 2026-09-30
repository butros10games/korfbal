"""Migration regressions run in their own real-migration lane, selected by marker."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex


PROJECT = Path(__file__).resolve().parents[2]
REPO = PROJECT.parents[2]
PROJECT_ROOT = str(PROJECT.relative_to(REPO))
# Tests that build or apply the historical migration graph.
MIGRATION_GRAPH = re.compile(
    r"MigrationExecutor|call_command\(\s*[\"']migrate[\"']|apps\.get_model\("
)


def _commands(target: str) -> list[list[str]]:
    config = json.loads((PROJECT / "project.json").read_text())["targets"][target]
    commands = [config["options"]["command"]]
    commands += [
        variant["command"]
        for variant in config.get("configurations", {}).values()
        if "command" in variant
    ]
    return [shlex.split(command) for command in commands]


def _marker(arguments: list[str]) -> str:
    return arguments[arguments.index("-m") + 1]


def test_every_migration_graph_module_is_marked() -> None:
    """Unmarked historical-graph tests would run only against --nomigrations."""
    unmarked = [
        str(path.relative_to(PROJECT))
        for path in sorted(PROJECT.rglob("test_*.py"))
        if "migrations" not in path.parts
        and MIGRATION_GRAPH.search(text := path.read_text())
        and "migration_regression" not in text
    ]

    assert unmarked == []


def test_migration_lane_selects_every_marked_test_with_real_migrations() -> None:
    """Newly marked tests are discovered by marker, not by a copied path list."""
    for arguments in _commands("test-migrations"):
        assert _marker(arguments) == "migration_regression"
        assert arguments[-1] == PROJECT_ROOT
        assert "--nomigrations" not in arguments
        assert "KORFBAL_TEST_DB_LANE=migrations" in arguments


def test_general_lane_excludes_migration_regressions() -> None:
    """The fast lane skips the real graph; the aggregate target needs both lanes."""
    for arguments in _commands("test-general"):
        assert _marker(arguments) == "not migration_regression"
        assert "--nomigrations" in arguments
        assert "KORFBAL_TEST_DB_LANE=general" in arguments
    config = json.loads((PROJECT / "project.json").read_text())["targets"]["test"]
    assert set(config["dependsOn"]) == {"test-general", "test-migrations"}
