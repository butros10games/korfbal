"""Behavior tests for the MVP award lifecycle."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from django.utils import timezone
import pytest

from apps.awards.models import MatchMvp, MatchMvpVote
from apps.awards.services import mvp as mvp_service
from apps.game_tracker.models import (
    GroupType,
    MatchPart,
    PlayerChange,
    PlayerGroup,
    Shot,
)

from .helpers import AwardsScenario


pytestmark = pytest.mark.django_db


def test_match_mvp_window_uses_latest_completed_part() -> None:
    """A completed period is authoritative over later tracker events."""
    scenario = AwardsScenario.create()
    candidate = scenario.player("candidate")
    now = timezone.now()
    earlier_end = now - timedelta(minutes=20)
    latest_end = now - timedelta(minutes=5)
    MatchPart.objects.create(
        match_data=scenario.tracker.match_data,
        part_number=1,
        start_time=earlier_end - timedelta(minutes=10),
        end_time=earlier_end,
    )
    MatchPart.objects.create(
        match_data=scenario.tracker.match_data,
        part_number=2,
        start_time=latest_end - timedelta(minutes=10),
        end_time=latest_end,
    )
    Shot.objects.create(
        player=candidate,
        match_data=scenario.tracker.match_data,
        team=scenario.tracker.home_team,
        time=now - timedelta(minutes=1),
    )

    award = mvp_service.get_or_create_match_mvp(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert award.finished_at == latest_end
    assert award.closes_at == latest_end + mvp_service.VOTING_WINDOW


def test_match_mvp_window_falls_back_to_latest_shot() -> None:
    """Matches without completed periods use the last recorded shot time."""
    scenario = AwardsScenario.create()
    candidate = scenario.player("candidate")
    earlier = timezone.now() - timedelta(minutes=8)
    latest = timezone.now() - timedelta(minutes=3)
    for shot_time in (earlier, latest):
        Shot.objects.create(
            player=candidate,
            match_data=scenario.tracker.match_data,
            team=scenario.tracker.home_team,
            time=shot_time,
        )

    award = mvp_service.get_or_create_match_mvp(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert award.finished_at == latest


def test_match_mvp_window_falls_back_to_latest_player_change() -> None:
    """A substitution timestamp is used when periods and shots are absent."""
    scenario = AwardsScenario.create()
    substitute = scenario.player("substitute")
    group_type = GroupType.objects.create(name=f"group-{scenario.prefix}")
    player_group = PlayerGroup.objects.create(
        match_data=scenario.tracker.match_data,
        team=scenario.tracker.home_team,
        starting_type=group_type,
        current_type=group_type,
    )
    latest = timezone.now() - timedelta(minutes=2)
    PlayerChange.objects.create(
        player_in=substitute,
        player_group=player_group,
        match_data=scenario.tracker.match_data,
        time=latest,
    )

    award = mvp_service.get_or_create_match_mvp(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert award.finished_at == latest


def test_match_mvp_window_uses_current_time_without_tracker_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An imported finished match still gets a bounded voting window."""
    scenario = AwardsScenario.create()
    finished_at = timezone.now() - timedelta(minutes=1)
    monkeypatch.setattr(mvp_service.timezone, "now", lambda: finished_at)

    award = mvp_service.get_or_create_match_mvp(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert award.finished_at == finished_at
    assert award.closes_at == finished_at + mvp_service.VOTING_WINDOW


def test_existing_open_window_is_shortened_but_never_extended() -> None:
    """Changing the configured duration only tightens a live legacy window."""
    scenario = AwardsScenario.create()
    finished_at = timezone.now() - timedelta(minutes=30)
    award = MatchMvp.objects.create(
        match=scenario.tracker.match,
        finished_at=finished_at,
        closes_at=finished_at + timedelta(hours=12),
    )

    returned = mvp_service.get_or_create_match_mvp(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert returned.id_uuid == award.id_uuid
    assert returned.finished_at == finished_at
    assert returned.closes_at == finished_at + mvp_service.VOTING_WINDOW

    returned.closes_at = finished_at + timedelta(hours=1)
    returned.save(update_fields=["closes_at", "updated_at"])
    unchanged = mvp_service.get_or_create_match_mvp(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )
    assert unchanged.closes_at == finished_at + timedelta(hours=1)


def test_roster_candidates_are_eligible_ordered_and_match_scoped() -> None:
    """Roster presence defines eligibility and home/away ordering."""
    scenario = AwardsScenario.create()
    away_zed = scenario.player("zed", team=scenario.tracker.away_team)
    home_bob = scenario.player("bob", team=scenario.tracker.home_team)
    home_alice = scenario.player(
        "alice",
        team=scenario.tracker.home_team,
        first_name="Alice",
        last_name="Example",
    )
    outsider = scenario.player("outsider")
    Shot.objects.create(
        player=outsider,
        match_data=scenario.tracker.match_data,
        team=scenario.tracker.home_team,
        time=timezone.now(),
    )

    candidates = mvp_service.build_mvp_candidates(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert [candidate.id_uuid for candidate in candidates] == [
        str(home_alice.id_uuid),
        str(home_bob.id_uuid),
        str(away_zed.id_uuid),
    ]
    assert [candidate.team_side for candidate in candidates] == [
        "home",
        "home",
        "away",
    ]
    assert candidates[0].display_name == "Alice Example"


def test_event_candidates_are_used_only_when_roster_is_empty() -> None:
    """Legacy matches can derive a de-duplicated candidate set from events."""
    scenario = AwardsScenario.create()
    shot_player = scenario.player("shot-player")
    substitute = scenario.player("substitute")
    group_type = GroupType.objects.create(name=f"group-{scenario.prefix}")
    player_group = PlayerGroup.objects.create(
        match_data=scenario.tracker.match_data,
        team=scenario.tracker.home_team,
        starting_type=group_type,
        current_type=group_type,
    )
    Shot.objects.create(
        player=shot_player,
        match_data=scenario.tracker.match_data,
        team=scenario.tracker.home_team,
        time=timezone.now(),
    )
    PlayerChange.objects.create(
        player_in=substitute,
        player_out=shot_player,
        player_group=player_group,
        match_data=scenario.tracker.match_data,
        time=timezone.now(),
    )

    candidates = mvp_service.build_mvp_candidates(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert {candidate.id_uuid for candidate in candidates} == {
        str(shot_player.id_uuid),
        str(substitute.id_uuid),
    }
    assert all(candidate.team_side is None for candidate in candidates)


def _open_award(scenario: AwardsScenario) -> MatchMvp:
    now = timezone.now()
    return MatchMvp.objects.create(
        match=scenario.tracker.match,
        finished_at=now - timedelta(minutes=10),
        closes_at=now + timedelta(hours=1),
    )


def test_authenticated_vote_updates_one_identity_record() -> None:
    """Changing an authenticated vote preserves its identity and row."""
    scenario = AwardsScenario.create()
    _open_award(scenario)
    candidate_a = scenario.player("candidate-a", team=scenario.tracker.home_team)
    candidate_b = scenario.player("candidate-b", team=scenario.tracker.away_team)
    voter = scenario.player("voter")

    original = mvp_service.cast_vote(
        match=scenario.tracker.match,
        match_data=scenario.tracker.match_data,
        voter=voter,
        candidate=candidate_a,
    )
    changed = mvp_service.cast_vote(
        match=scenario.tracker.match,
        match_data=scenario.tracker.match_data,
        voter=voter,
        candidate=candidate_b,
    )

    assert changed.id_uuid == original.id_uuid
    assert changed.candidate_id == candidate_b.id_uuid
    assert changed.voter_token is None
    assert MatchMvpVote.objects.filter(match=scenario.tracker.match).count() == 1


def test_anonymous_vote_updates_one_token_record() -> None:
    """Changing a cookie-backed vote preserves its token identity and row."""
    scenario = AwardsScenario.create()
    _open_award(scenario)
    candidate_a = scenario.player("candidate-a", team=scenario.tracker.home_team)
    candidate_b = scenario.player("candidate-b", team=scenario.tracker.away_team)
    token = str(uuid4())

    original = mvp_service.cast_vote_anon(
        match=scenario.tracker.match,
        match_data=scenario.tracker.match_data,
        voter_token=token,
        candidate=candidate_a,
    )
    changed = mvp_service.cast_vote_anon(
        match=scenario.tracker.match,
        match_data=scenario.tracker.match_data,
        voter_token=token,
        candidate=candidate_b,
    )

    assert changed.id_uuid == original.id_uuid
    assert changed.candidate_id == candidate_b.id_uuid
    assert changed.voter_id is None
    assert changed.voter_token == UUID(token)
    assert MatchMvpVote.objects.filter(match=scenario.tracker.match).count() == 1


def test_vote_rejects_closed_window_and_nonparticipant() -> None:
    """Neither expiry nor an unrelated player can be bypassed at service level."""
    scenario = AwardsScenario.create()
    candidate = scenario.player("candidate", team=scenario.tracker.home_team)
    outsider = scenario.player("outsider")
    voter = scenario.player("voter")
    award = _open_award(scenario)

    with pytest.raises(ValueError, match="Invalid MVP candidate"):
        mvp_service.cast_vote(
            match=scenario.tracker.match,
            match_data=scenario.tracker.match_data,
            voter=voter,
            candidate=outsider,
        )

    award.closes_at = timezone.now() - timedelta(seconds=1)
    award.save(update_fields=["closes_at", "updated_at"])
    with pytest.raises(ValueError, match="Voting is closed"):
        mvp_service.cast_vote(
            match=scenario.tracker.match,
            match_data=scenario.tracker.match_data,
            voter=voter,
            candidate=candidate,
        )


def test_publish_selects_stable_lowest_uuid_on_vote_tie() -> None:
    """Equal vote totals resolve to the same winner on every database."""
    scenario = AwardsScenario.create()
    candidate_a = scenario.player("candidate-a", team=scenario.tracker.home_team)
    candidate_b = scenario.player("candidate-b", team=scenario.tracker.away_team)
    voters = [scenario.player(f"voter-{index}") for index in range(4)]
    closed_at = timezone.now() - timedelta(minutes=1)
    MatchMvp.objects.create(
        match=scenario.tracker.match,
        finished_at=closed_at - mvp_service.VOTING_WINDOW,
        closes_at=closed_at,
    )
    for voter, candidate in zip(
        voters,
        (candidate_a, candidate_a, candidate_b, candidate_b),
        strict=True,
    ):
        MatchMvpVote.objects.create(
            match=scenario.tracker.match,
            voter=voter,
            candidate=candidate,
        )

    published = mvp_service.ensure_mvp_published(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )
    first_published_at = published.published_at
    published_again = mvp_service.ensure_mvp_published(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    expected = min((candidate_a, candidate_b), key=lambda player: player.id_uuid)
    assert published.mvp_player_id == expected.id_uuid
    assert first_published_at is not None
    assert published_again.published_at == first_published_at


def test_closed_award_without_votes_is_published_without_winner() -> None:
    """A finished voting window reaches a terminal state even with no votes."""
    scenario = AwardsScenario.create()
    closed_at = timezone.now() - timedelta(minutes=1)
    MatchMvp.objects.create(
        match=scenario.tracker.match,
        finished_at=closed_at - mvp_service.VOTING_WINDOW,
        closes_at=closed_at,
    )

    published = mvp_service.ensure_mvp_published(
        scenario.tracker.match,
        scenario.tracker.match_data,
    )

    assert published.mvp_player is None
    assert published.published_at is not None


def test_status_payload_keeps_authenticated_identity_and_vote_breakdown() -> None:
    """Status exposes candidate metadata, totals, and only the requested identity."""
    scenario = AwardsScenario.create()
    _open_award(scenario)
    home = scenario.player(
        "home",
        team=scenario.tracker.home_team,
        first_name="Home",
        last_name="Candidate",
    )
    away = scenario.player("away", team=scenario.tracker.away_team)
    voter = scenario.player("voter")
    anon_token = str(uuid4())
    MatchMvpVote.objects.create(
        match=scenario.tracker.match,
        voter=voter,
        candidate=home,
    )
    MatchMvpVote.objects.create(
        match=scenario.tracker.match,
        voter_token=anon_token,
        candidate=away,
    )

    payload = mvp_service.build_match_mvp_status_payload(
        match=scenario.tracker.match,
        match_data=scenario.tracker.match_data,
        voter=voter,
        anon_voter_token=anon_token,
    )

    assert payload["available"] is True
    assert payload["open"] is True
    assert payload["user_vote"] == {"candidate_id_uuid": str(home.id_uuid)}
    assert payload["mvp"] is None
    assert [candidate["team_side"] for candidate in payload["candidates"]] == [
        "home",
        "away",
    ]
    assert payload["candidates"][0]["display_name"] == "Home Candidate"
    assert {
        row["candidate"]["id_uuid"]: row["votes"] for row in payload["vote_breakdown"]
    } == {str(home.id_uuid): 1, str(away.id_uuid): 1}
