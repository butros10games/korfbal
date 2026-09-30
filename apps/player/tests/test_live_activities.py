"""Live Activity props, delivery bookkeeping, and the publish hook."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from operator import itemgetter
from pathlib import Path
from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
import pytest

from apps.game_tracker.tasks import publish_public_live_snapshot
from apps.kwt_common.models import BackgroundJob
from apps.player.application.ports import LiveActivityDeliveryError
from apps.player.models.live_activity import MatchLiveActivity
from apps.player.services.live_activities import (
    LIVE_ACTIVITY_NAME,
    LiveActivityNotFoundError,
    LiveActivityPushResult,
    build_live_activity_payload,
    build_live_activity_props,
    clock_label,
    end_live_activity,
    live_activity_stale_at,
    period_label,
    push_live_activities,
    push_live_activities_for_match,
    register_live_activity,
)
from apps.schedule.tests.match_api_test_support import create_match_graph


NOW = datetime(2026, 9, 29, 19, 32, 15, tzinfo=UTC)
# The Expo app's matchLiveActivityProps.test.ts checks the same cases.
CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[6] / "fixtures/korfbal/live-activity.json"
    ).read_text(encoding="utf-8")
)


def iso(value: datetime) -> str:
    """Instants as the widget props carry them (JavaScript ``toISOString``)."""
    utc = value.astimezone(UTC).isoformat(timespec="milliseconds")
    return utc.replace("+00:00", "Z")


TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


def snapshot(**overrides: object) -> dict[str, Any]:
    """Build a published live payload for a running second half."""
    base: dict[str, Any] = {
        "status": "active",
        "current_part": 2,
        "parts": 2,
        "paused": False,
        "timer": {
            "type": "active",
            "time": (NOW - timedelta(minutes=12, seconds=30)).isoformat(),
            "length": 1800,
            "pause_length": 90,
        },
        "score": {"home": 12, "away": 9},
        "live_revision": 7,
    }
    base.update(overrides)
    return base


class FakeClient:
    """Record pushes and fail selected tokens."""

    configured = True

    def __init__(
        self, failures: dict[str, LiveActivityDeliveryError] | None = None
    ) -> None:
        """Remember which tokens should fail."""
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.failures = failures or {}

    def send(self, *, token: str, payload: dict[str, Any]) -> None:
        """Record the push or raise the configured failure."""
        if token in self.failures:
            raise self.failures[token]
        self.sent.append((token, payload))


def test_clock_label_subtracts_pauses_and_caps_at_period_length() -> None:
    """Running clocks drop paused time; a paused clock freezes at the pause."""
    running = snapshot()["timer"]
    assert clock_label(running, now=NOW) == "11'"
    paused = {
        **running,
        "type": "pause",
        "calc_to": (NOW - timedelta(minutes=5)).isoformat(),
    }
    assert clock_label(paused, now=NOW) == "6'"
    overtime = {**running, "time": (NOW - timedelta(minutes=40)).isoformat()}
    assert clock_label(overtime, now=NOW) == "30'"
    assert not clock_label({"type": "deactivated"}, now=NOW)


def test_period_label_uses_dutch_halves_and_periods() -> None:
    """Two-part matches say helft; other formats count periods."""
    assert period_label(status="active", current_part=1, parts=2) == "1e helft"
    assert period_label(status="active", current_part=2, parts=2) == "2e helft"
    assert period_label(status="active", current_part=3, parts=4) == "3e periode"
    assert period_label(status="upcoming", current_part=1, parts=2) == (
        "Nog niet begonnen"
    )
    assert period_label(status="finished", current_part=2, parts=2) == "Afgelopen"


def test_payload_matches_expo_widgets_contract() -> None:
    """Props travel as a JSON string under the activity name; ends dismiss later."""
    props = build_live_activity_props(
        snapshot=snapshot(), home_name="DVO", away_name="PKC", now=NOW
    )
    payload = build_live_activity_payload(props, event="update", now=NOW)
    aps = payload["aps"]
    assert aps["event"] == "update"
    assert aps["timestamp"] == int(NOW.timestamp())
    assert aps["content-state"]["name"] == "MatchLiveActivity"
    assert json.loads(aps["content-state"]["props"]) == {
        "homeName": "DVO",
        "awayName": "PKC",
        "homeScore": 12,
        "awayScore": 9,
        "status": "active",
        "periodLabel": "2e helft",
        "clockLabel": "11'",
        "paused": False,
        "clockStartAt": iso(NOW - timedelta(minutes=11)),
        "clockEndAt": iso(NOW + timedelta(minutes=19)),
        "clockPausedAt": "",
    }
    assert "dismissal-date" not in aps
    assert aps["stale-date"] == int((NOW + timedelta(minutes=29)).timestamp())
    ended = build_live_activity_payload(
        build_live_activity_props(
            snapshot=snapshot(status="finished"),
            home_name="DVO",
            away_name="PKC",
            now=NOW,
        ),
        event="end",
        now=NOW,
    )
    assert ended["aps"]["dismissal-date"] == int(NOW.timestamp()) + 3600
    assert "stale-date" not in ended["aps"]
    final = json.loads(ended["aps"]["content-state"]["props"])
    assert not final["clockLabel"]
    assert not final["clockStartAt"]


def test_paused_clock_carries_the_pause_instant() -> None:
    """A paused period keeps its start and tells iOS where the clock stopped."""
    paused_at = NOW - timedelta(minutes=5)
    props = build_live_activity_props(
        snapshot=snapshot(
            paused=True,
            timer={
                **snapshot()["timer"],
                "type": "pause",
                "calc_to": paused_at.isoformat(),
            },
        ),
        home_name="DVO",
        away_name="PKC",
        now=NOW,
    )
    assert props.paused
    assert props.clock_paused_at == iso(paused_at)
    assert props.clock_start_at == iso(NOW - timedelta(minutes=11))


def test_quiet_play_stays_fresh_until_the_period_should_have_ended() -> None:
    """A running clock is trusted to its scheduled end; silence stales sooner."""

    def stale_at(**overrides: object) -> datetime:
        props = build_live_activity_props(
            snapshot=snapshot(**overrides), home_name="DVO", away_name="PKC", now=NOW
        )
        return live_activity_stale_at(props, now=NOW)

    # 19 minutes remain in the half, plus the grace for a late end registration.
    assert stale_at() == NOW + timedelta(minutes=29)
    overrun = {**snapshot()["timer"], "time": (NOW - timedelta(minutes=40)).isoformat()}
    assert stale_at(timer=overrun) == NOW + timedelta(minutes=10)
    paused = {
        **snapshot()["timer"],
        "type": "pause",
        "calc_to": (NOW - timedelta(minutes=2)).isoformat(),
    }
    assert stale_at(paused=True, timer=paused) == NOW + timedelta(minutes=30)
    assert stale_at(timer={"type": "deactivated"}) == NOW + timedelta(minutes=30)


@pytest.mark.django_db
def test_register_transfers_rotated_tokens_and_end_is_owner_scoped() -> None:
    """A token belongs to whoever registered it last; others cannot end it."""
    graph = create_match_graph(prefix="Live activity register")
    users = get_user_model()
    owner = users.objects.create_user(username="la-owner")
    other = users.objects.create_user(username="la-other")
    activity, created = register_live_activity(
        user_id=owner.pk, match_id=str(graph.match.pk), push_token=TOKEN_A
    )
    assert created
    assert activity.is_active
    same, created_again = register_live_activity(
        user_id=other.pk, match_id=str(graph.match.pk), push_token=TOKEN_A
    )
    assert not created_again
    assert same.pk == activity.pk
    assert same.user_id == other.pk
    with pytest.raises(LiveActivityNotFoundError):
        end_live_activity(user_id=owner.pk, push_token=TOKEN_A)
    end_live_activity(user_id=other.pk, push_token=TOKEN_A)
    same.refresh_from_db()
    assert not same.is_active


@pytest.mark.django_db
def test_push_updates_every_active_phone_once_per_revision() -> None:
    """Delivered revisions are remembered; dead tokens stop; ends deactivate."""
    graph = create_match_graph(prefix="Live activity push")
    user = get_user_model().objects.create_user(username="la-push")
    match_id = str(graph.match.pk)
    register_live_activity(user_id=user.pk, match_id=match_id, push_token=TOKEN_A)
    register_live_activity(user_id=user.pk, match_id=match_id, push_token=TOKEN_B)
    client = FakeClient({
        TOKEN_B: LiveActivityDeliveryError(
            status_code=410, reason="Unregistered", permanent=True
        )
    })
    result = push_live_activities(
        match_id=match_id,
        snapshot=snapshot(),
        team_names=("DVO", "PKC"),
        client=client,
        now=NOW,
    )
    assert result == LiveActivityPushResult(sent=1, failed=1)
    assert [token for token, _ in client.sent] == [TOKEN_A]
    assert not MatchLiveActivity.objects.get(push_token=TOKEN_B).is_active
    # The same revision again is a no-op for the phone that already has it.
    repeat = push_live_activities(
        match_id=match_id,
        snapshot=snapshot(),
        team_names=("DVO", "PKC"),
        client=client,
        now=NOW,
    )
    assert repeat == LiveActivityPushResult(skipped=1)
    # A transient failure keeps the token for the next revision.
    flaky = FakeClient({
        TOKEN_A: LiveActivityDeliveryError(
            status_code=503, reason="ServiceUnavailable", permanent=False
        )
    })
    push_live_activities(
        match_id=match_id,
        snapshot=snapshot(live_revision=8),
        team_names=("DVO", "PKC"),
        client=flaky,
        now=NOW,
    )
    assert MatchLiveActivity.objects.get(push_token=TOKEN_A).is_active
    finished = push_live_activities(
        match_id=match_id,
        snapshot=snapshot(status="finished", live_revision=9),
        team_names=("DVO", "PKC"),
        client=client,
        now=NOW,
    )
    assert finished == LiveActivityPushResult(sent=1, ended=1)
    assert client.sent[-1][1]["aps"]["event"] == "end"
    assert not MatchLiveActivity.objects.filter(is_active=True).exists()


@pytest.mark.django_db
def test_match_push_reads_nothing_without_watchers() -> None:
    """Snapshot and team reads only happen when a phone is registered."""
    graph = create_match_graph(prefix="Live activity idle")
    reads: list[str] = []

    def read_snapshot(match_id: str) -> dict[str, Any]:
        reads.append(match_id)
        return snapshot()

    def read_names(match_id: str) -> tuple[str, str]:
        return ("DVO", "PKC")

    client = FakeClient()
    match_id = str(graph.match.pk)
    assert (
        push_live_activities_for_match(
            match_id=match_id,
            client=client,
            read_snapshot=read_snapshot,
            read_team_names=read_names,
        )
        == LiveActivityPushResult()
    )
    assert reads == []
    user = get_user_model().objects.create_user(username="la-watch")
    register_live_activity(user_id=user.pk, match_id=match_id, push_token=TOKEN_A)
    result = push_live_activities_for_match(
        match_id=match_id,
        client=client,
        read_snapshot=read_snapshot,
        read_team_names=read_names,
    )
    assert result.sent == 1
    assert reads == [match_id]
    props = json.loads(client.sent[0][1]["aps"]["content-state"]["props"])
    assert (props["homeName"], props["awayName"]) == ("DVO", "PKC")


@pytest.mark.django_db(transaction=True)
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_publication_enqueues_live_activity_push_only_for_watched_matches() -> None:
    """The durable snapshot job fans out to phones showing the match."""
    graph = create_match_graph(prefix="Live activity hook")
    match_id = str(graph.match.pk)
    task = "apps.player.tasks.push_match_live_activities"
    with patch("apps.kwt_common.services.jobs.publish_job"):
        publish_public_live_snapshot(match_id=match_id)
        assert not BackgroundJob.objects.filter(task=task).exists()
        user = get_user_model().objects.create_user(username="la-hook")
        register_live_activity(user_id=user.pk, match_id=match_id, push_token=TOKEN_A)
        publish_public_live_snapshot(match_id=match_id)
        publish_public_live_snapshot(match_id=match_id)
    jobs = list(BackgroundJob.objects.filter(task=task))
    assert len(jobs) == 1
    assert jobs[0].args == [match_id]


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=itemgetter("name"))
def test_props_match_the_shared_app_contract(case: dict[str, Any]) -> None:
    """APNs updates and the app's own updates render identical content state."""
    props = build_live_activity_props(
        snapshot=case["live"],
        home_name=case["homeName"],
        away_name=case["awayName"],
        now=datetime.fromisoformat(case["now"]),
    )

    assert props.to_dict() == case["props"]


def test_activity_name_matches_the_shared_app_contract() -> None:
    """APNs content state must name the widget the app registered."""
    assert CONTRACT["activityName"] == LIVE_ACTIVITY_NAME
