"""Retain sporting identity evidence for conditional native fixture updates."""

from datetime import UTC
from typing import Any

from apps.competition.models import Match, MatchMembership
from apps.game_tracker.models import (
    MatchData,
    MatchGuestPlayer,
    MatchPart,
    MatchPlayer,
    PlayerGroup,
    Shot,
    StartingPlayerAssignment,
)
from apps.schedule.models import Match as NativeMatch


BASELINE_VERSION = 1


def native_fixture(match: NativeMatch) -> dict[str, Any]:
    """Snapshot the actual native sporting identity."""
    return {
        "version": BASELINE_VERSION,
        "season_id": str(match.season_id),
        "home_team_id": str(match.home_team_id),
        "away_team_id": str(match.away_team_id),
        "pool_id": str(match.pool_id) if match.pool_id else None,
        "starts_at": match.start_time.astimezone(UTC).isoformat(),
    }


def desired_fixture(
    row: Match,
    home: object,
    away: object,
    pool: object,
    *,
    season_id: object | None = None,
) -> dict[str, Any]:
    """Resolve provider IDs through already-linked native entities only."""
    return {
        "version": BASELINE_VERSION,
        "season_id": str(season_id if season_id is not None else row.season_id),
        "home_team_id": str(home),
        "away_team_id": str(away),
        "pool_id": str(pool) if pool else None,
        "starts_at": row.starts_at.astimezone(UTC).isoformat(),
    }


def locally_owned(tracker: MatchData) -> bool:
    """Conservatively protect legacy revisions until local provenance is explicit."""
    return bool(
        tracker.live_revision
        or tracker.command_sequence
        or tracker.event_sequence
        or tracker.status == "active"
    )


def fixture_dependencies(row: Match, tracker: MatchData) -> dict[str, int]:
    """Count sporting dependencies without materializing people or event payloads."""
    return {
        "source_selections": MatchMembership.objects.filter(match=row).count(),
        "native_players": MatchPlayer.objects.filter(match_data=tracker).count(),
        "lineup_assignments": StartingPlayerAssignment.objects.filter(
            match_data=tracker
        ).count(),
        "lineup_memberships": PlayerGroup.players.through.objects.filter(
            playergroup__match_data=tracker
        ).count(),
        "guest_players": MatchGuestPlayer.objects.filter(match_data=tracker).count(),
        "periods": MatchPart.objects.filter(match_data=tracker).count(),
        "shots": Shot.objects.filter(match_data=tracker).count(),
        "pending_notification": int(row.schedule_notification_id is not None),
    }


def fixture_decision(
    row: Match,
    tracker: MatchData,
    current: dict[str, Any],
    desired: dict[str, Any],
    *,
    dependencies: dict[str, int] | None = None,
) -> str:
    """Accept a change only against a retained importer-owned baseline."""
    baseline = row.published_schedule.get("fixture")
    participants_changed = any(
        current[key] != desired[key] for key in ("home_team_id", "away_team_id")
    )
    if current == desired:
        decision = "unchanged"
    elif locally_owned(tracker):
        decision = "protected_tracking"
    elif not row.local_created:
        decision = "protected_manual"
    elif not isinstance(baseline, dict) or baseline.get("version") != BASELINE_VERSION:
        decision = "legacy_baseline_review"
    elif baseline != current:
        decision = "protected_native_change"
    elif current["season_id"] != desired["season_id"]:
        decision = "season_review"
    elif participants_changed and any(
        (dependencies or fixture_dependencies(row, tracker)).values()
    ):
        decision = "protected_dependencies"
    else:
        decision = "safe_update"
    return decision
