"""Conservative, entity-scoped season coverage for compatible public choices."""

from apps.schedule.models import Season


def coverage_payload(season: Season, *, has_entity_data: bool) -> dict[str, object]:
    """Expose uncertainty without allowing a global flag to hide real content.

    A known entity season is evidence of available content, never proof that all
    its fixtures (or every club's fixtures) were supplied. Legacy flags retain
    their empty-season behavior until the additive migration is deployed.
    """
    coverage = season.data_coverage
    reason = season.coverage_reason
    if coverage == "unknown" and season.data_unavailable:
        coverage = "unavailable"
    if coverage == "unavailable" and has_entity_data:
        coverage = "partial"
        reason = (
            reason or "Available entity data; tested source discovery is incomplete."
        )
    if not has_entity_data and coverage in {"unknown", "partial"}:
        reason = (
            "No data is recorded for this entity in this period; "
            "source coverage is not proven complete."
        )
    return {
        "data_coverage": coverage,
        "coverage_reason": reason,
        "data_unavailable": coverage == "unavailable" and not has_entity_data,
    }
