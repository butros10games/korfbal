"""Bounded source observations with conservative native result ownership."""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from apps.competition.models import Match
from apps.competition.services.seasons import SeasonResolver
from apps.game_tracker.domain.source_results import (
    SourceResult,
    normalize_source_result,
)
from apps.game_tracker.models import MatchPart, Shot


TRACKER = "local_match__tracker_data__"


def _native_season(row: dict[str, Any], resolver: SeasonResolver) -> UUID | None:
    """Route the source season like the publisher: by discipline and period."""
    sport = row["home_team__sport"]
    if sport != row["away_team__sport"]:
        return None
    phase = row["pool__phase"] or ""
    if (
        row["pool_id"] is not None
        and not phase
        and resolver.splits(row["season_id"], sport)
    ):
        return None
    return resolver.resolve(row["season_id"], sport, phase)


def _provider_owned(
    row: dict[str, Any], history: set[str], resolver: SeasonResolver
) -> bool:
    """Mirror the first-release publisher's conservative local protection."""
    if (
        row[f"{TRACKER}id_uuid"] is None
        or row[f"{TRACKER}live_revision"]
        or row[f"{TRACKER}command_sequence"]
        or row[f"{TRACKER}event_sequence"]
        or row[f"{TRACKER}status"] == "active"
    ):
        return False
    current = {
        "status": row[f"{TRACKER}status"],
        "home": row[f"{TRACKER}home_score"],
        "away": row[f"{TRACKER}away_score"],
    }
    if row["published_state"] and current != row["published_state"]:
        return False
    if (
        row["home_team__group__local_team_id"] != row["local_match__home_team_id"]
        or row["away_team__group__local_team_id"] != row["local_match__away_team_id"]
        or _native_season(row, resolver) != row["local_match__season_id"]
    ):
        # Use the publisher's canonical sporting identity. Protected pool or
        # kickoff edits still permit results for the same sides and season;
        # participant/season corrections must await native reconciliation.
        return False
    if row["external_id"].startswith("archive:") and not row["local_created"]:
        return False
    if (
        row[f"{TRACKER}score_source"] not in {"knkv", "archive"}
        and not row["local_created"]
    ):
        pristine = current == {"status": "upcoming", "home": 0, "away": 0}
        if not pristine or str(row["local_match_id"]) in history:
            return False
    return True


def source_results(match_ids: Iterable[str]) -> dict[str, SourceResult]:
    """Read selected linked fixtures; never scan a season or historic catalogue."""
    selected = set(match_ids)
    if not selected:
        return {}
    rows = list(
        Match.objects.filter(local_match_id__in=selected).values(
            "local_match_id",
            "external_id",
            "status",
            "home_score",
            "away_score",
            "local_created",
            "published_state",
            "season_id",
            "pool_id",
            "pool__phase",
            "home_team__sport",
            "away_team__sport",
            "home_team__group__local_team_id",
            "away_team__group__local_team_id",
            "local_match__home_team_id",
            "local_match__away_team_id",
            "local_match__season_id",
            *(
                f"{TRACKER}{field}"
                for field in (
                    "id_uuid",
                    "status",
                    "score_source",
                    "home_score",
                    "away_score",
                    "live_revision",
                    "command_sequence",
                    "event_sequence",
                )
            ),
        )
    )
    # Legacy, pre-existing fixtures with no imported score owner additionally
    # require proof that no tracker history exists. Batch only those candidates.
    legacy = [
        row["local_match_id"]
        for row in rows
        if not row["local_created"]
        and row[f"{TRACKER}score_source"] not in {"knkv", "archive"}
    ]
    history = (
        {
            str(match_id)
            for match_id in Shot.objects
            .filter(match_data__match_link_id__in=legacy)
            .values_list("match_data__match_link_id", flat=True)
            .union(
                MatchPart.objects.filter(
                    match_data__match_link_id__in=legacy
                ).values_list("match_data__match_link_id", flat=True)
            )
        }
        if legacy
        else set()
    )
    # One bounded read of the selected scopes' publication routing.
    resolver = SeasonResolver(row["season_id"] for row in rows)
    return {
        str(row["local_match_id"]): normalize_source_result(
            status=row["status"],
            home_score=row["home_score"],
            away_score=row["away_score"],
            source="archive" if row["external_id"].startswith("archive:") else "knkv",
            display_authority=(
                "provider" if _provider_owned(row, history, resolver) else "local"
            ),
        )
        for row in rows
    }
