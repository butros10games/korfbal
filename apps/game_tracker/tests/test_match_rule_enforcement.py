"""Tracker commands and state read the same match rule profile."""

from __future__ import annotations

from typing import Any

import pytest

from apps.competition.domain.match_rules import RuleContext, resolve_rules
from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.domain.match_rules import (
    ASSUMED_DEFAULT,
    MatchRules,
    rules_from_snapshot,
)
from apps.game_tracker.models import GroupType, MatchData, PlayerChange, Timeout
from apps.game_tracker.services.match_rule_profiles import (
    APPLIED,
    PENDING_REVIEW,
    apply_rule_profile,
)
from apps.game_tracker.services.tracker_commands import TrackerCommandError
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_match_part,
    create_player_group,
    create_tracker_match,
)
from apps.player.services.goal_song_manifest import build_goal_song_manifest


pytestmark = [pytest.mark.postgres_parity, pytest.mark.django_db]

A_LIMIT = 8


def profile(category: str, colour: str | None = None) -> MatchRules:
    """Resolve a verified 2026-2027 senior eight-player profile for a category."""
    return resolve_rules(
        RuleContext(
            edition=2026,
            discipline="outdoor",
            category=category,
            age_group="senior",
            colour=colour,
            playing_format="eight",
        )
    )


def live_match(prefix: str, rules: MatchRules | None) -> TrackerMatchContext:
    """Create an active match under a rule profile, with an opponent reserve."""
    tracker = create_tracker_match(prefix=prefix)
    if rules is not None:
        assert apply_rule_profile(tracker.match_data, rules) == APPLIED
    MatchData.objects.filter(pk=tracker.match_data.pk).update(status="active")
    tracker.match_data.refresh_from_db()
    create_match_part(match_data=tracker.match_data)
    reserve = create_player_group(
        match_data=tracker.match_data,
        team=tracker.away_team,
        group_type=GroupType.objects.get_or_create(name="Reserve")[0],
    )
    PlayerChange.objects.bulk_create(
        PlayerChange(player_group=reserve, match_data=tracker.match_data)
        for _ in range(A_LIMIT)
    )
    return tracker


def state(tracker: TrackerMatchContext) -> dict[str, Any]:
    """Read the reporting team's full tracker state."""
    return get_tracker_state(
        tracker.match, team=tracker.home_team, goal_audio=build_goal_song_manifest
    )


def substitute(tracker: TrackerMatchContext) -> None:
    """Register an opponent substitution."""
    apply_tracker_command(
        tracker.match,
        team=tracker.home_team,
        payload={"command": "substitute_against_reg"},
    )


def test_b_category_allows_unlimited_substitutions() -> None:
    """KNKV 9.6.2: B-category substitutions are unlimited, and shown as such."""
    tracker = live_match("B unlimited", profile("b", colour="red"))
    substitute(tracker)
    payload = state(tracker)
    assert payload["substitutions"]["against"] == A_LIMIT + 1
    assert payload["substitutions"]["max"] is None
    assert payload["rules"]["substitutions"] == "unlimited"


@pytest.mark.parametrize("rules", [profile("a"), None])
def test_a_category_and_legacy_matches_keep_eight(rules: MatchRules | None) -> None:
    """KNKV 9.6.1 for A/top; an unresolved match keeps the legacy limit."""
    tracker = live_match(f"A limit {rules is None}", rules)
    with pytest.raises(TrackerCommandError) as error:
        substitute(tracker)
    assert error.value.code == "max_substitutions"
    assert state(tracker)["substitutions"]["max"] == A_LIMIT


def test_timeouts_follow_the_profile() -> None:
    """KNKV 9.4: time-outs exist in topkorfbal and A, not in B."""
    b_match = live_match("B timeouts", profile("b", colour="red"))
    with pytest.raises(TrackerCommandError) as error:
        apply_tracker_command(
            b_match.match,
            team=b_match.home_team,
            payload={"command": "timeout", "for_team": True},
        )
    assert error.value.code == "timeouts_not_applicable"
    assert state(b_match)["timeouts"]["max"] == 0
    assert not Timeout.objects.filter(match_data=b_match.match_data).exists()

    a_match = live_match("A timeouts", profile("a"))
    assert state(a_match)["timeouts"]["max"] == 2  # noqa: PLR2004 - KNKV 9.4


def test_tracked_match_keeps_its_profile_until_reviewed() -> None:
    """A different profile for an active match is held for review."""
    tracker = live_match("Pending", profile("a"))
    before = (tracker.match_data.parts, tracker.match_data.part_length)
    shorter = resolve_rules(
        RuleContext(edition=2026, category="a", age_group="U15", discipline="indoor")
    )
    tracker.match_data.refresh_from_db()
    assert apply_rule_profile(tracker.match_data, shorter) == PENDING_REVIEW
    tracker.match_data.refresh_from_db()
    assert (tracker.match_data.parts, tracker.match_data.part_length) == before
    assert tracker.match_data.rules_pending["rules"]["periods"] == [25, 25]


def test_empty_snapshot_is_the_explicit_legacy_assumption() -> None:
    """Existing matches keep working and say that their rules were assumed."""
    rules = rules_from_snapshot({}, parts=2, part_length=1800)
    assert rules.source == ASSUMED_DEFAULT
    assert rules.assumed
    assert not rules.duration_resolved
    assert rules.regulation_minutes == 60  # noqa: PLR2004 - legacy 2 x 30
    assert rules.effective_substitution_limit() == A_LIMIT
