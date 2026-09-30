"""Cross-app dependency policy for Korfbal Django apps.

Model relationships are measured separately: ORM foreign keys between apps are
intentional and may form cycles. Everything else must go through each app's
declared public interface, and the application-layer graph must stay acyclic.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path


APPS_DIR = Path(__file__).resolve().parents[2]
APPS = frozenset(path.parent.name for path in APPS_DIR.glob("*/apps.py"))
# Shared like models: pure rules and constants that import no services.
SHARED_LAYERS = frozenset({"models", "domain"})
INNER_LAYERS = frozenset({"application", "domain", "queries", "services"})
# Only these layers bind production implementations or adapt transports.
ENTRYPOINT_LAYERS = frozenset({
    "admin",
    "api",
    "composition",
    "management",
    "realtime",
    "signals",
    "tasks",
})
ENTRYPOINT_ONLY_INTERFACES = frozenset({"composition"})
PROVIDER_DEPTH = 2  # apps.<provider> precedes the provider module path

# Modules another app may import, per providing app. Adding an entry is an
# explicit ownership decision reviewed alongside the dependency it enables.
PUBLIC_INTERFACES: dict[str, frozenset[str]] = {
    "awards": frozenset({"services", "services.mvp"}),
    "club": frozenset({"api.serializers"}),
    "competition": frozenset({
        "domain.rosters",
        "services.classification",
        "services.match_prediction",
        "services.schedule_notifications",
    }),
    "game_tracker": frozenset({
        "application.ports",
        "composition",
        "queries.match_summaries",
        "services.event_editor",
        "services.event_reconciliation",
        "services.live_update_signal_control",
        "services.live_updates",
        "services.match_events",
        "services.match_impact",
        "services.match_impacts_payload",
        "services.match_mutations",
        "services.match_stats_payload",
        "services.match_timeline_payload",
        "services.player_designation",
        "services.player_groups",
        "services.player_statistics",
        "services.timeline_reads",
        "services.tracker_access",
        "services.tracker_commands",
    }),
    "kwt_common": frozenset({
        "admin_base",
        "admin_filters",
        "api.base",
        "api.pagination",
        "api.params",
        "api.permissions",
        "services.jobs",
    }),
    "player": frozenset({
        "api.serializers",
        "application.ports",
        "composition",
        "media_paths",
        "privacy",
        "services.goal_song",
        "services.goal_song_manifest",
        "services.live_activities",
        "services.match_notifications",
        "services.player_queries",
        "services.player_song_queries",
        "services.player_songs",
        "services.upload_validation",
        "services.web_push",
    }),
    "schedule": frozenset({"queries.seasons"}),
    "team": frozenset({"api.serializers", "services.roster_history"}),
    "tournament": frozenset({"composition", "services.cups"}),
    "video_analysis": frozenset({
        "composition",
        "services.match_video",
        "services.match_video_annotations",
        "services.match_video_playlists",
    }),
}

# Application-layer dependencies between apps (excluding models and pure domain
# modules). The set must match the code exactly and remain acyclic.
SERVICE_DEPENDENCIES: dict[str, frozenset[str]] = {
    "competition": frozenset({"game_tracker", "player", "team", "tournament"}),
    "player": frozenset({"awards", "game_tracker", "schedule"}),
    "team": frozenset({"game_tracker", "player", "schedule"}),
    "video_analysis": frozenset({"kwt_common"}),
}


@dataclass(frozen=True, slots=True)
class CrossAppImport:
    """One import of another Korfbal app's module."""

    source: Path
    line: int
    app: str
    layer: str
    provider: str
    target: str

    def __str__(self) -> str:
        """Locate the import for readable assertion output.

        Returns:
            The import location and target.

        """
        return (
            f"{self.source.relative_to(APPS_DIR)}:{self.line} imports "
            f"apps.{self.provider}.{self.target}"
        )


def _target(module: str, alias: str) -> list[str]:
    """Return ``apps.<provider>.<module>`` parts, expanding submodule imports."""
    parts = module.split(".")
    candidate = APPS_DIR.joinpath(*parts[1:], alias)
    if candidate.with_suffix(".py").exists() or (candidate / "__init__.py").exists():
        return [*parts, alias]
    return parts


@cache
def _cross_app_imports() -> tuple[CrossAppImport, ...]:
    return tuple(_scan())


def _scan() -> list[CrossAppImport]:
    found: list[CrossAppImport] = []
    for path in sorted(APPS_DIR.rglob("*.py")):
        relative = path.relative_to(APPS_DIR).parts
        if {"tests", "migrations", "__pycache__"}.intersection(relative):
            continue
        app, layer = relative[0], Path(relative[1]).stem
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".") for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [_target(node.module, alias.name) for alias in node.names]
            else:
                continue
            for parts in modules:
                if (
                    len(parts) > PROVIDER_DEPTH
                    and parts[0] == "apps"
                    and parts[1] in APPS
                    and parts[1] != app
                ):
                    found.append(
                        CrossAppImport(
                            path,
                            node.lineno,
                            app,
                            layer,
                            parts[1],
                            ".".join(parts[2:]),
                        )
                    )
    return found


def _non_model_imports() -> list[CrossAppImport]:
    return [item for item in _cross_app_imports() if item.target != "models"]


def test_cross_app_imports_use_declared_public_interfaces() -> None:
    """Another app's internals (adapters, private services) stay private."""
    violations = [
        str(item)
        for item in _non_model_imports()
        if not item.target.startswith("models.")
        and item.target not in PUBLIC_INTERFACES.get(item.provider, frozenset())
    ]

    assert violations == []


def test_composition_roots_are_imported_only_by_entrypoints() -> None:
    """Only transports, tasks and other composition roots bind implementations."""
    violations = [
        str(item)
        for item in _non_model_imports()
        if item.target.split(".")[0] in ENTRYPOINT_ONLY_INTERFACES
        and item.layer not in ENTRYPOINT_LAYERS
    ]

    assert violations == []


def test_shared_infrastructure_depends_on_no_domain_app() -> None:
    """kwt_common stays generic: no other app's services, queries or APIs."""
    violations = [
        str(item)
        for item in _non_model_imports()
        if item.app == "kwt_common" and not item.target.startswith("models")
    ]

    assert violations == []


def test_domain_modules_import_only_models_and_domain_rules() -> None:
    """Pure domain modules can be shared because they reach no services."""
    violations = [
        str(item)
        for item in _cross_app_imports()
        if item.layer == "domain" and item.target.split(".")[0] not in SHARED_LAYERS
    ]

    assert violations == []


def _service_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for item in _non_model_imports():
        if (
            item.layer in INNER_LAYERS
            and item.target.split(".")[0] not in SHARED_LAYERS
        ):
            graph[item.app].add(item.provider)
    return graph


def test_service_dependencies_match_the_declared_graph() -> None:
    """New application-layer dependencies between apps are explicit decisions."""
    actual = {app: frozenset(deps) for app, deps in _service_graph().items()}

    assert actual == SERVICE_DEPENDENCIES


def _cycles(graph: dict[str, frozenset[str]]) -> list[list[str]]:
    found: list[list[str]] = []

    def visit(node: str, path: list[str]) -> None:
        for neighbour in sorted(graph.get(node, frozenset())):
            if neighbour in path:
                found.append([*path[path.index(neighbour) :], neighbour])
            elif len(path) < len(graph) + 1:
                visit(neighbour, [*path, neighbour])

    for start in sorted(graph):
        visit(start, [start])
    return found


def test_declared_service_graph_is_acyclic() -> None:
    """Application services form a DAG; cycles are broken with ports."""
    assert _cycles(SERVICE_DEPENDENCIES) == []


def test_cycle_detection_reports_mutual_service_dependencies() -> None:
    """The acyclicity check fails on the kind of cycle it guards against."""
    assert _cycles({"a": frozenset({"b"}), "b": frozenset({"a"})})
