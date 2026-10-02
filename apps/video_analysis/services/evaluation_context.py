"""Season, discipline and format context of review recordings for evaluation.

Recordings of one sporting match (several cameras or split files) form one
group, so their frames always land in the same split. Context comes from the
linked native match; an unlinked recording keeps every field unknown.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from apps.game_tracker.models import MatchData
from apps.video_analysis.engine.splits import held_out_season_splits
from apps.video_analysis.models import Recording


UNKNOWN = "unknown"


def recording_group(recording: Recording) -> str:
    """Return the leakage group: the linked match, else the recording's group."""
    if recording.match_id:
        return f"match:{recording.match_id}"
    return str(recording.metadata.get("split_group") or recording.source_id)


def group_contexts(workspace_id: object | None = None) -> dict[str, dict[str, Any]]:
    """Describe every recording group with explicit unknowns.

    Returns:
        Group to edition, discipline, phase, players per team and recordings.

    """
    recordings = Recording.objects.select_related("match__season").order_by("pk")
    if workspace_id is not None:
        recordings = recordings.filter(workspace_id=workspace_id)
    rules = {
        row.match_link_id: row.match_rules()
        for row in MatchData.objects.filter(
            match_link_id__in=[r.match_id for r in recordings if r.match_id]
        )
    }
    groups: dict[str, dict[str, Any]] = {}
    for recording in recordings:
        group = recording_group(recording)
        context = groups.setdefault(
            group,
            {
                "edition": None,
                "discipline": UNKNOWN,
                "phase": UNKNOWN,
                "players_per_team": UNKNOWN,
                "recordings": 0,
            },
        )
        context["recordings"] += 1
        match = recording.match
        if match is None:
            continue
        season = match.season.context
        profile = rules.get(match.pk)
        context.update(
            edition=season.edition,
            discipline=season.discipline or UNKNOWN,
            phase=season.phase or UNKNOWN,
            players_per_team=(profile.players_per_team if profile else None) or UNKNOWN,
        )
    return groups


def evaluation_report(
    held_out: set[int], workspace_id: object | None = None
) -> dict[str, Any]:
    """Preview a held-out-season assignment and its context coverage.

    Returns:
        Coverage counts and the proposed per-group split; nothing is written.

    """
    groups = group_contexts(workspace_id)
    report: dict[str, Any] = {
        "groups": len(groups),
        "unknown": {
            field: sum(1 for row in groups.values() if row[field] in {None, UNKNOWN})
            for field in ("edition", "discipline", "phase", "players_per_team")
        },
        "by_context": dict(
            Counter(
                f"{row['edition']}|{row['discipline']}|{row['players_per_team']}"
                for row in groups.values()
            )
        ),
    }
    if held_out:
        assignment = held_out_season_splits(groups, held_out)
        report["held_out_editions"] = sorted(held_out)
        report["splits"] = dict(Counter(assignment.values()))
        report["assignment"] = assignment
    return report
