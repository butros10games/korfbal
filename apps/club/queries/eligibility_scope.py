"""Separate the displayed season from the competition history eligibility needs.

Clubs see full-year outdoor seasons as their two halves. Eligibility must not
follow that display split: a continuous outdoor competition keeps its whole
history (and its winter-break exception) whichever half is shown, while
independent competition periods keep separate histories.

KNKV Reglement van Wedstrijden art. 21: https://www.knkv.nl/kennisbank/reglement-van-wedstrijden/
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db.models import Max, Min, Q
from django.utils import timezone

from apps.competition.models import Match as SourceMatch
from apps.schedule.domain.competition_context import FULL_SEASON
from apps.schedule.models import Match, Season
from apps.schedule.queries.seasons import folded_full_year, season_edition


@dataclass(frozen=True)
class EligibilityScope:
    """Seasons whose rosters and fixtures form one eligibility history."""

    display: Season | None
    seasons: tuple[Season, ...]
    continuous: Season | None = None
    winter_break: tuple[datetime, datetime] | None = None

    def as_payload(self) -> dict[str, Any]:
        """Describe the resolved scope next to the requested display season."""
        return {
            "display_season_id": str(self.display.pk) if self.display else None,
            "season_ids": [str(season.pk) for season in self.seasons],
            "continuous_season_id": (
                str(self.continuous.pk) if self.continuous else None
            ),
            "winter_break": (
                {
                    "start": self.winter_break[0].isoformat(),
                    "end": self.winter_break[1].isoformat(),
                }
                if self.winter_break
                else None
            ),
        }


def eligibility_scope(season: Season | None, team_ids: list[str]) -> EligibilityScope:
    """Resolve the competition history behind a displayed season.

    ``team_ids`` are the club's teams; the winter break is read from their own
    continuous-competition fixtures rather than assumed from a calendar date.

    Returns:
        The display season with the seasons and break eligibility reads.

    """
    if season is None:
        return EligibilityScope(display=None, seasons=())
    folded = folded_full_year(season)
    continuous = (
        folded.whole
        if folded is not None
        else season
        if season.context.phase == FULL_SEASON
        else None
    )
    seasons = (season, folded.whole) if folded is not None else (season,)
    return EligibilityScope(
        display=season,
        seasons=seasons,
        continuous=continuous,
        winter_break=winter_break(continuous, team_ids) if continuous else None,
    )


def winter_break(
    season: Season, team_ids: list[str]
) -> tuple[datetime, datetime] | None:
    """Return the gap between a continuous competition's two halves.

    The break runs from the club's last fixture before 1 January to its first
    fixture after it; 1 January only separates the halves, it is not assumed to
    be a competition boundary.
    """
    edition = season_edition(season)
    if edition is None or not team_ids:
        return None
    turn = datetime(edition + 1, 1, 1, tzinfo=timezone.get_current_timezone())
    played = Match.objects.filter(season=season).filter(
        Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids)
    )
    bounds = played.aggregate(
        before=Max("start_time", filter=Q(start_time__lt=turn)),
        after=Min("start_time", filter=Q(start_time__gte=turn)),
    )
    if bounds["before"] is None or bounds["after"] is None:
        return None
    return bounds["before"], bounds["after"]


def match_periods(match_ids: list[UUID]) -> dict[str, str]:
    """Return each fixture's competition period key, '' when unknown.

    Imported fixtures follow their poule (with an indoor part when known);
    native fixtures follow their season's stored phase.
    """
    periods: dict[str, str] = {}
    for local_id, phase, part in SourceMatch.objects.filter(
        local_match_id__in=match_ids
    ).values_list("local_match_id", "pool__phase", "pool__competition_part"):
        if phase:
            periods[str(local_id)] = f"{phase}:{part}" if part else phase
    for match_id, phase in Match.objects.filter(pk__in=match_ids).values_list(
        "pk", "season__phase"
    ):
        periods.setdefault(str(match_id), phase or "")
    return periods
