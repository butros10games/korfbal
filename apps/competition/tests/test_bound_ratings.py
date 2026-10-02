"""Seeded ratings and predictions follow source-to-native season bindings."""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone
import pytest

from apps.competition.models import (
    Allocation,
    Match as SourceMatch,
    Pool,
    RatingConfiguration,
)
from apps.competition.services.importer import Importer
from apps.competition.services.match_prediction import (
    match_prediction,
    rating_prediction,
)
from apps.competition.services.published_ratings import (
    configure_ratings,
    fingerprint,
    published_ratings,
)
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.seasons import INDOOR, OUTDOOR, configure_seasons
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload
from apps.competition.tests.test_rating_preview import PARAMETERS, create_baseline
from apps.schedule.models import Season


pytestmark = pytest.mark.django_db


def scope() -> Season:
    """Create an annual provider scope."""
    return Season.objects.create(
        name="Bound 2026-2027", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )


def publish() -> None:
    """Run one publication pass."""
    publish_catalogue(schedule_changes=RecordingScheduleChanges())


def test_bound_indoor_source_match_is_found() -> None:
    """An annual source fixture published into its indoor season keeps its link."""
    annual = scope()
    indoor = configure_seasons(annual, 2026)[INDOOR]
    row = match_payload()
    row["HomeTeam"]["SportId"] = INDOOR
    row["AwayTeam"]["SportId"] = INDOOR
    Importer(annual, timezone.now()).apply(
        "club_results", "CT1", {"MatchResult": [row]}
    )
    publish()
    source = SourceMatch.objects.get()
    native = source.local_match
    assert native is not None
    assert native.season_id == indoor.pk != source.season_id
    RatingConfiguration.objects.create(
        season=indoor,
        effective_at=native.start_time - timedelta(days=1),
        active=True,
        b_scale=400,
        b_k_factor=32,
    )
    # The link is found; the class is simply not settled for this synthetic label.
    assert rating_prediction(native)["reason"] == "unresolved_competition"


def test_seeded_prediction_through_a_period_binding() -> None:
    """Allocation baselines, results and classes resolve in the period's season."""
    annual = scope()
    configure_seasons(annual, 2026, split_outdoor=True)
    baseline = create_baseline(annual, sport=OUTDOOR)
    publish()
    source = SourceMatch.objects.select_related("local_match__season", "pool").get()
    native = source.local_match
    assert native is not None
    assert native.season.phase == "autumn"
    assert source.pool is not None
    assert source.pool.phase == "autumn"
    allocation = Allocation.objects.select_related("competition_class__edition").first()
    assert allocation is not None
    assert allocation.competition_class_id == source.pool.competition_class_id
    configure_ratings(native.season, [baseline.pk], PARAMETERS, apply=True)
    assert match_prediction(native)["status"] == "seeded"
    configuration = RatingConfiguration.objects.get(season=native.season)
    ratings = published_ratings(configuration)
    assert {row["external_id"] for row in ratings["results"]} == {"T1", "T2"}
    assert ratings["metadata"]["used_results"] == 1


def test_rating_cache_key_follows_routing_changes() -> None:
    """Re-routing moves populations without touching match rows: keys change."""
    annual = scope()
    configure_seasons(annual, 2026, split_outdoor=True)
    baseline = create_baseline(annual, sport=OUTDOOR)
    publish()
    native = SourceMatch.objects.select_related("local_match__season").get().local_match
    assert native is not None
    configure_ratings(native.season, [baseline.pk], PARAMETERS, apply=True)
    configuration = RatingConfiguration.objects.get(season=native.season)
    now = timezone.now()
    before = fingerprint(configuration, now, [])
    # A reviewed repair changes the poule's recorded period.
    Pool.objects.update(phase="full_season")
    assert fingerprint(configuration, now, []) != before
