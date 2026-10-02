"""Explicit context for forecast evaluation, with honest unknowns."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.competition.models import (
    Match as SourceMatch,
    Pool,
)
from apps.competition.offline.context_evaluation import (
    UNKNOWN,
    context_coverage,
    held_out_split,
)
from apps.competition.queries.forecast_export import (
    CONTEXT_SCHEMA,
    competition_context,
    export_rows,
)
from apps.competition.services.importer import Importer
from apps.competition.tests.test_importer import match_payload
from apps.schedule.tests.season_builders import playing_season


def row(edition: int | None, **values: object) -> dict[str, object]:
    """Return one schema 3 export row."""
    return {
        "edition": edition,
        "discipline": "indoor",
        "period": "indoor",
        "playing_format": "eight",
        "category": "a",
        "gender": "mixed",
        **values,
    }


def test_held_out_editions_never_train_on_later_seasons() -> None:
    """Earlier editions train, held-out ones test, later or unknown are excluded."""
    rows = [row(2023), row(2024), row(2025), row(None)]
    split = held_out_split(rows, {2024})
    assert [item["edition"] for item in split["train"]] == [2023]
    assert [item["edition"] for item in split["test"]] == [2024]
    assert [item["edition"] for item in split["excluded"]] == [2025, None]
    with pytest.raises(ValueError, match="held-out"):
        held_out_split(rows, set())


def test_context_coverage_counts_unknowns() -> None:
    """Missing context stays visible instead of joining a known context."""
    coverage = context_coverage([row(2024), row(None, period=None)])
    assert coverage["rows"] == len([1, 2])
    assert coverage["unknown"]["edition"] == 1
    assert coverage["unknown"]["period"] == 1
    assert {item["edition"] for item in coverage["contexts"]} == {"2024", UNKNOWN}


@pytest.mark.django_db
def test_export_context_is_opt_in(tmp_path: Path) -> None:
    """Default exports keep their schema; schema 3 adds edition and period."""
    season = playing_season("spring", 2025)
    Importer(season, timezone.now()).apply(
        "club_results",
        "CT1",
        {
            "MatchResult": [
                {**match_payload(), "MatchDateTime": "2026-04-04T13:30:00+0200"}
            ]
        },
    )
    Pool.objects.update(phase="spring")
    source = SourceMatch.objects.select_related("season", "pool").get()
    assert competition_context(source) == {
        "edition": 2025,
        "period": "spring",
        "part": None,
    }
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert export_rows(str(season.pk), now)["schema"] == 1
    report = export_rows(str(season.pk), now, with_context=True)
    assert report["schema"] == CONTEXT_SCHEMA
    path = tmp_path / "export.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    call_command("report_forecast_context", "--input", str(path))
