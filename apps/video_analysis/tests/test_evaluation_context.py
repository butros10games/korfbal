"""Held-out-season video evaluation keeps recordings of one match together."""

from __future__ import annotations

from datetime import UTC, date, datetime

from django.contrib.auth import get_user_model
import pytest

from apps.club.models import Club
from apps.schedule.models import Match, Season
from apps.team.models import Team
from apps.video_analysis.engine.splits import held_out_season_splits, split_for_group
from apps.video_analysis.models import Recording, Workspace
from apps.video_analysis.services.evaluation_context import (
    evaluation_report,
    group_contexts,
)


EDITION = 2025


@pytest.mark.django_db
def test_video_groups_keep_one_match_together() -> None:
    """Every recording of a match shares one group, context and split."""
    season = Season.objects.create(
        name="Video 2025-2026",
        start_date=date(2025, 10, 1),
        end_date=date(2026, 6, 30),
        edition=EDITION,
        discipline="indoor",
        phase="indoor",
    )
    home = Team.objects.create(name="Video 1", club=Club.objects.create(name="V1"))
    away = Team.objects.create(name="Video 2", club=Club.objects.create(name="V2"))
    match = Match.objects.create(
        home_team=home,
        away_team=away,
        season=season,
        start_time=datetime(2025, 11, 1, 14, tzinfo=UTC),
    )
    owner = get_user_model().objects.create_user(username="video-owner")
    workspace = Workspace.objects.create(owner=owner, slug="evaluation")
    for source in ("camera-a", "camera-b"):
        Recording.objects.create(workspace=workspace, source_id=source, match=match)
    Recording.objects.create(
        workspace=workspace, source_id="unlinked", metadata={"split_group": "loose"}
    )
    groups = group_contexts(workspace.pk)
    linked = groups[f"match:{match.pk}"]
    assert linked["recordings"] == len(["camera-a", "camera-b"])
    assert linked["edition"] == EDITION
    assert linked["discipline"] == "indoor"
    assert groups["loose"]["edition"] is None
    assignment = held_out_season_splits(groups, {EDITION})
    assert assignment[f"match:{match.pk}"] == "test"
    assert assignment["loose"] == "pool"
    report = evaluation_report({EDITION}, workspace.pk)
    assert report["unknown"]["edition"] == 1
    assert report["splits"] == {"test": 1, "pool": 1}
    # The default whole-group assignment is unchanged.
    assert split_for_group("loose") in {"train", "validation", "test"}
