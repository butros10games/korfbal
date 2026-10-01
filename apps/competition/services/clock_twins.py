"""Merge imported fixtures into the manual fixture they duplicate by a clock offset.

Publication matched fixtures by exact kickoff only, so a manually entered match
stored an hour or two off (Dutch local time saved as UTC) gained an imported
twin. The repair moves the provider link to the manual fixture, which keeps its
tracked data, and deletes the untouched imported copy.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from apps.competition.models import Match
from apps.competition.services.publishing import CLOCK_SHIFTS
from apps.game_tracker.models import MatchData, MatchPart, Shot
from apps.schedule.models import Match as AppMatch


# Relations a fixture created by publication always has; anything else is use.
OWNED_RELATIONS = {"competition_identity", "tracker_data"}


def find_pairs() -> list[tuple[Match, AppMatch]]:
    """Pair each unlinked manual fixture with its single imported clock twin.

    Returns:
        Source records linked to an untouched imported copy, with the manual
        fixture they should link to instead.

    """
    pairs = []
    for manual in AppMatch.objects.filter(competition_identity__isnull=True):
        twins = list(
            Match.objects.filter(
                local_created=True,
                local_match__season_id=manual.season_id,
                local_match__home_team_id=manual.home_team_id,
                local_match__away_team_id=manual.away_team_id,
                local_match__start_time__in=[
                    manual.start_time + shift for shift in CLOCK_SHIFTS
                ],
            ).select_related("local_match")
        )
        if len(twins) == 1 and untouched(twins[0].local_match):
            pairs.append((twins[0], manual))
    return pairs


def untouched(fixture: AppMatch) -> bool:
    """Tell whether an imported fixture carries nothing beyond published data."""
    for relation in fixture._meta.related_objects:
        if relation.get_accessor_name() in OWNED_RELATIONS:
            continue
        if relation.related_model.objects.filter(**{
            relation.field.name: fixture
        }).exists():
            return False
    tracker = MatchData.objects.filter(match_link=fixture).first()
    return tracker is None or not (
        tracker.live_revision
        or tracker.command_sequence
        or tracker.event_sequence
        or Shot.objects.filter(match_data=tracker).exists()
        or MatchPart.objects.filter(match_data=tracker).exists()
    )


def describe(pairs: list[tuple[Match, AppMatch]]) -> list[dict[str, str]]:
    """Summarize pairs without people or credentials.

    Returns:
        One row per pair with both kickoffs.

    """
    return [
        {
            "source": source.external_id,
            "imported_kickoff": source.local_match.start_time.isoformat(),
            "manual_kickoff": manual.start_time.isoformat(),
            "manual_fixture": str(manual.pk),
        }
        for source, manual in pairs
    ]


def merge(pairs: list[tuple[Match, AppMatch]]) -> int:
    """Relink each source record to its manual fixture and drop the copy.

    Returns:
        The number of merged pairs.

    """
    merged = 0
    for source, manual in pairs:
        with transaction.atomic():
            copy = source.local_match
            locked = Match.objects.select_for_update(of=("self",)).filter(
                Q(local_match=manual) | Q(pk=source.pk, local_match=copy),
            )
            if len(locked) != 1 or not untouched(copy):
                continue
            # Republish against the manual fixture: its tracked result is kept.
            Match.objects.filter(pk=source.pk).update(
                local_match=manual,
                local_created=False,
                published_state={},
                published_at=None,
            )
            copy.delete()
            merged += 1
    return merged
