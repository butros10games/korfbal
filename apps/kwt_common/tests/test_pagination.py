"""Tests for stable API pagination ordering."""

from apps.kwt_common.api.pagination import ensure_totally_ordered
from apps.team.models import Team


def test_ensure_totally_ordered_adds_primary_key_tie_breaker() -> None:
    """Django 6.1 should identify and repair ambiguous page ordering."""
    queryset = Team.objects.order_by("name")

    assert not queryset.totally_ordered

    ordered_queryset = ensure_totally_ordered(queryset)

    assert ordered_queryset.totally_ordered
    assert ordered_queryset.query.order_by == ("name", "id_uuid")


def test_ensure_totally_ordered_preserves_unique_ordering() -> None:
    """An already deterministic queryset should remain unchanged."""
    queryset = Team.objects.order_by("id_uuid")

    assert queryset.totally_ordered
    assert ensure_totally_ordered(queryset) is queryset
