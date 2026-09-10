"""Retain observed cup membership without inventing rounds or bracket decisions."""

from apps.competition.models import CupCompetition, CupFixture, Match


# Exact labels observed in the KNKV feeds, including their regional distinctions.
OBSERVED_CUPS = frozenset({
    "AP Buiten-beker",
    "Damesbeker",
    "Jan Mulder-beker",
    "U17 beker NRD",
    "U17 beker ZWT",
    "U19 beker NRD",
    "U19 beker ZWT",
})


def import_cup_fixture(match: Match) -> CupFixture | None:
    """Idempotently retain a known provider classification and the native match link."""
    pool = match.pool
    if pool is None or pool.class_name not in OBSERVED_CUPS:
        return None
    competition, _ = CupCompetition.objects.get_or_create(
        season=match.season,
        name=pool.class_name,
        sport=pool.sport,
    )
    fixture, _ = CupFixture.objects.get_or_create(
        match=match, defaults={"competition": competition}
    )
    return fixture


def import_observed_cup_fixture(match: Match, data: dict) -> None:
    """Avoid extra relation reads for ordinary league fixtures."""
    if data.get("Pool", {}).get("ClassName") in OBSERVED_CUPS:
        import_cup_fixture(match)
