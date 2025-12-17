"""Helpers for computing and persisting match scores.

Why this exists:
- The live tracker derives the score from shots.
- Overview/summary endpoints prefer persisted MatchData.home_score/away_score.
- Historically some code paths marked a match as finished without persisting the
  final score, causing finished matches to show 0-0 in the UI.

This module provides a single, consistent way to (re)compute scores from shots
and persist them to MatchData.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast
from uuid import UUID

from django.db.models import Count, F, Q

from apps.game_tracker.models import MatchData, Shot


def compute_scores_for_matchdata_ids(
    match_data_ids: Iterable[UUID],
) -> dict[UUID, tuple[int, int]]:
    """Compute (home_score, away_score) per MatchData from scored shots.

    Notes:
        - We count scored shots grouped by MatchData.
        - Home/away is determined by comparing Shot.team with the match's
          home_team/away_team.

    """
    ids = list(match_data_ids)
    if not ids:
        return {}

    rows = (
        Shot.objects.filter(
            match_data_id__in=ids,
            scored=True,
            team__isnull=False,
        )
        .values("match_data_id")
        .annotate(
            home=Count(
                "id_uuid",
                filter=Q(team=F("match_data__match_link__home_team")),
            ),
            away=Count(
                "id_uuid",
                filter=Q(team=F("match_data__match_link__away_team")),
            ),
        )
    )

    scores: dict[UUID, tuple[int, int]] = dict.fromkeys(ids, (0, 0))
    for row in rows:
        match_data_id = cast(UUID, row["match_data_id"])
        scores[match_data_id] = (int(row.get("home") or 0), int(row.get("away") or 0))

    return scores


def persist_matchdata_scores(match_data: MatchData) -> tuple[int, int]:
    """Recompute and persist scores onto MatchData.

    Returns:
        tuple[int, int]: (home_score, away_score)

    """
    computed = compute_scores_for_matchdata_ids([cast(UUID, match_data.id_uuid)]).get(
        cast(UUID, match_data.id_uuid),
        (0, 0),
    )

    match_data.home_score, match_data.away_score = computed
    match_data.save(update_fields=["home_score", "away_score"])
    return computed
