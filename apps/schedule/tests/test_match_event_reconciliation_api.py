"""API coverage for match-event reconciliation review."""

from datetime import timedelta
from http import HTTPStatus

from django.test.client import Client
from django.utils import timezone
import pytest

from apps.game_tracker.models import (
    MatchEvent,
    MatchEventReconciliation,
    MatchEventReconciliationDecision,
    Shot,
)
from apps.schedule.tests.match_api_test_support import create_editor_context


@pytest.mark.django_db
@pytest.mark.parametrize("changed_index", [0, 1], ids=["canonical", "duplicate"])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_merge_rejects_changed_candidate_events(
    client: Client, changed_index: int, change: str
) -> None:
    """An old review must not discard newer event corrections or surviving goals."""
    context = create_editor_context(client, username="stale-reconciliation")
    graph = context.graph
    shots = [
        Shot.objects.create(
            match_data=graph.match_data,
            match_part=context.match_part,
            player=context.actor,
            team=graph.home_team,
            scored=True,
            time=timezone.now() + timedelta(seconds=offset),
        )
        for offset in (0, 5)
    ]
    events = [
        MatchEvent.objects.get(source_type="shot", source_id=shot.pk) for shot in shots
    ]
    candidate = MatchEventReconciliation.objects.create(
        match_data=graph.match_data,
        first_event=events[0],
        second_event=events[1],
        confidence=75,
        reason="Close independent reports",
    )
    changed = shots[changed_index]
    if change == "delete":
        changed.delete()
    else:
        changed.scored = False
        changed.save(update_fields=["scored"])
    graph.match_data.refresh_from_db()
    revision = graph.match_data.live_revision
    before = list(
        Shot.objects.filter(match_data=graph.match_data).order_by("pk").values()
    )
    event_count = MatchEvent.objects.filter(match_data=graph.match_data).count()

    pending = client.get(f"/api/matches/{graph.match.pk}/events/reconciliations/")
    assert pending.status_code == HTTPStatus.OK
    assert pending.json() == {"reconciliations": []}

    response = client.post(
        f"/api/matches/{graph.match.pk}/events/reconciliations/{candidate.pk}/resolve/",
        data={"decision": "merge", "canonical_event_id": str(events[0].pk)},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert (
        list(Shot.objects.filter(match_data=graph.match_data).order_by("pk").values())
        == before
    )
    assert not MatchEventReconciliationDecision.objects.filter(
        reconciliation=candidate
    ).exists()
    graph.match_data.refresh_from_db()
    assert graph.match_data.live_revision == revision
    assert MatchEvent.objects.filter(match_data=graph.match_data).count() == event_count


@pytest.mark.django_db
def test_coach_can_review_and_separate_event_reconciliation(client: Client) -> None:
    """Let a coach confirm that two reported goals are distinct."""
    context = create_editor_context(client, username="reconciliation-reviewer")
    graph = context.graph
    shots = [
        Shot.objects.create(
            match_data=graph.match_data,
            match_part=context.match_part,
            player=context.actor,
            team=graph.home_team,
            scored=True,
            time=timezone.now() + timedelta(seconds=offset),
        )
        for offset in (0, 5)
    ]
    events = [
        MatchEvent.objects.get(source_type="shot", source_id=shot.pk) for shot in shots
    ]
    candidate = MatchEventReconciliation.objects.create(
        match_data=graph.match_data,
        first_event=events[0],
        second_event=events[1],
        confidence=75,
        reason="Close independent reports",
    )
    reconciliations_url = f"/api/matches/{graph.match.id_uuid}/events/reconciliations/"

    pending = client.get(reconciliations_url)
    assert pending.status_code == HTTPStatus.OK
    assert pending.json()["reconciliations"][0]["id_uuid"] == str(candidate.pk)

    resolved = client.post(
        f"{reconciliations_url}{candidate.pk}/resolve/",
        data={"decision": "separate", "reason": "Confirmed as two goals"},
        content_type="application/json",
    )
    assert resolved.status_code == HTTPStatus.OK
    assert resolved.json()["decision"] == "separate"
    assert client.get(reconciliations_url).json() == {"reconciliations": []}
