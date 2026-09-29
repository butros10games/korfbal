"""Service-backed tests must run, not skip, in the PostgreSQL/Redis lane."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
import yaml


PROJECT = Path(__file__).resolve().parents[3]
REPO = PROJECT.parents[2]
LANE = REPO / ".depot/workflows/korfbal-postgres.yml"
# A test that checks for one of these services is only meaningful in the lane.
SERVICE_GATES = re.compile(
    r"connection\.vendor|PUBLIC_LIVE_TEST_REDIS_URL|KORFBAL_TEST_BROKER_URL"
)


def test_every_service_gated_module_is_selected_by_the_service_lane() -> None:
    """New PostgreSQL/Redis/broker gates cannot silently skip in every CI lane."""
    unmarked = [
        str(path.relative_to(PROJECT))
        for path in sorted((PROJECT / "apps").rglob("test_*.py"))
        if SERVICE_GATES.search(text := path.read_text())
        and "pytest.mark.service_backed" not in text
    ]

    assert unmarked == []


def test_service_lane_runs_every_gated_module_and_forbids_skips() -> None:
    """The lane provides every service, covers every gated module and never skips."""
    workflow = yaml.safe_load(LANE.read_text())
    job = workflow["jobs"]["concurrency"]
    lanes = [
        {**job["env"], **step.get("env", {}), "run": step["run"]}
        for step in job["steps"]
        if "KORFBAL_REQUIRE_SERVICES" in step.get("env", {})
    ]
    marked = next(lane for lane in lanes if "-m 'service_backed" in lane["run"])
    worker = next(lane for lane in lanes if "-m celery_worker" in lane["run"])
    # Worker tests leave the marker run only for their own required process.
    assert "service_backed and not celery_worker" in marked["run"]
    for lane in lanes:
        assert lane["DJANGO_TEST_USE_POSTGRES"] == "1"
        assert lane["KORFBAL_REQUIRE_SERVICES"] == "1"
    assert marked["PUBLIC_LIVE_TEST_REDIS_URL"].startswith("redis://")
    assert worker["KORFBAL_TEST_BROKER_URL"].startswith("redis://")
    assert worker["KORFBAL_TEST_BROKER_URL"] != marked["PUBLIC_LIVE_TEST_REDIS_URL"]


def _project_conftest() -> ModuleType:
    """Return the Korfbal conftest module pytest already loaded.

    Returns:
        The loaded conftest module.

    """
    target = PROJECT / "conftest.py"
    return next(
        module
        for module in list(sys.modules.values())
        if Path(getattr(module, "__file__", "") or "") == target
    )


def _report(item_marked: bool, *, outcome: str, xfail: bool = False) -> SimpleNamespace:
    report = SimpleNamespace(
        outcome=outcome,
        skipped=outcome == "skipped",
        longrepr=("test.py", 1, "Skipped: Requires PostgreSQL row locks"),
    )
    if xfail:
        report.wasxfail = "expected"
    item = SimpleNamespace(
        get_closest_marker=lambda name: (
            object() if item_marked and name == "service_backed" else None
        )
    )
    generator = _project_conftest().pytest_runtest_makereport(item, None)
    next(generator)
    with pytest.raises(StopIteration):
        generator.send(SimpleNamespace(get_result=lambda: report))
    return report


@pytest.mark.parametrize(
    ("required", "marked", "xfail", "expected"),
    [
        (True, True, False, "failed"),
        (True, False, False, "skipped"),
        (True, True, True, "skipped"),
        (False, True, False, "skipped"),
    ],
)
def test_required_service_skips_fail_only_in_the_lane(
    monkeypatch: pytest.MonkeyPatch,
    required: bool,
    marked: bool,
    xfail: bool,
    expected: str,
) -> None:
    """Only a service-backed skip in the lane becomes a failure."""
    monkeypatch.setattr(_project_conftest(), "REQUIRE_SERVICES", required)

    report = _report(marked, outcome="skipped", xfail=xfail)

    assert report.outcome == expected
    if expected == "failed":
        assert "Requires PostgreSQL row locks" in cast(str, report.longrepr)
