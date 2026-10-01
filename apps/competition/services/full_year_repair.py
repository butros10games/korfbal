"""Re-import poules that history import split over an edition's two outdoor halves.

A poule that plays both halves is one competition. History import first routed
its results by date into the autumn and spring seasons, so neither half showed
the full results or matching standings. The repair removes the imported copies
and queues the poule again; the import now places it in the full-year season.
Fixtures that are linked to native activity are never removed.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.competition.models import (
    HistoricalResource,
    Match,
    MatchMembership,
    Pool,
    PoolEntry,
    Team,
    TeamGroup,
)
from apps.competition.services.clock_twins import untouched
from apps.competition.services.history_editions import (
    edition_scopes,
    season_names,
)
from apps.schedule.models import (
    Match as AppMatch,
    Season,
)
from apps.team.models import TeamData


def halves(edition: int) -> tuple[Season, Season] | None:
    """Return the edition's autumn and spring seasons, if both exist."""
    autumn, _, spring = season_names(edition)
    seasons = {season.name.lower(): season for season in edition_scopes(edition)}
    if autumn.lower() in seasons and spring.lower() in seasons:
        return seasons[autumn.lower()], seasons[spring.lower()]
    return None


def split_poules(edition: int) -> list[str]:
    """List poule IDs that have imported rows in both outdoor halves.

    Returns:
        Provider poule IDs, in ascending order.

    """
    seasons = halves(edition)
    if seasons is None:
        return []
    return sorted(
        Pool.objects
        .filter(season__in=seasons)
        .values("external_id")
        .annotate(halves=Count("season", distinct=True))
        .filter(halves=2)
        .values_list("external_id", flat=True),
        key=int,
    )


def repair_edition(edition: int, *, apply: bool) -> dict[str, Any]:
    """Preview, or remove and requeue, every split poule of one edition.

    Returns:
        Split poule IDs and, when applied, what was removed or skipped.

    """
    poules = split_poules(edition)
    result: dict[str, Any] = {"edition": edition, "split_poules": len(poules)}
    seasons = halves(edition)
    if not apply or seasons is None:
        return result
    counts: Counter[str] = Counter()
    skipped = []
    for poule in poules:
        if requeue_poule(edition, seasons, poule, counts):
            counts["poules_requeued"] += 1
        else:
            skipped.append(poule)
    counts.update(remove_orphan_teams(seasons))
    result.update(counts=dict(counts), skipped=skipped)
    return result


@transaction.atomic
def requeue_poule(
    edition: int, seasons: tuple[Season, Season], poule: str, counts: Counter[str]
) -> bool:
    """Remove one split poule's imported rows and queue it for import again.

    Returns:
        Whether the poule was removed; False leaves it untouched.

    """
    pools = list(
        Pool.objects
        .select_for_update(of=("self",))
        .filter(season__in=seasons, external_id=poule)
        .select_related("local_pool")
    )
    matches = list(
        Match.objects
        .select_for_update(of=("self",))
        .filter(pool__in=pools)
        .select_related("local_match")
    )
    natives = [match.local_match for match in matches if match.local_match]
    if any(
        not match.local_created for match in matches if match.local_match_id
    ) or not all(untouched(native) for native in natives):
        return False
    identities = [match.external_id for match in matches]
    Match.objects.filter(pk__in=[match.pk for match in matches]).update(
        local_match=None
    )
    AppMatch.objects.filter(pk__in=[native.pk for native in natives]).delete()
    HistoricalResource.objects.filter(
        season__in=seasons, kind__in=("match", "lineup"), source_id__in=identities
    ).delete()
    Match.objects.filter(pk__in=[match.pk for match in matches]).delete()
    local_pools = [pool.local_pool for pool in pools if pool.local_pool is not None]
    Pool.objects.filter(pk__in=[pool.pk for pool in pools]).delete()
    for local in local_pools:
        if not AppMatch.objects.filter(pool=local).exists():
            local.delete()
    requeued = HistoricalResource.objects.filter(
        season__name__iexact=season_names(edition)[1],
        provider="app",
        kind="edition_pool",
        source_id=poule,
    ).update(
        state="pending",
        coverage="unknown",
        reason="",
        etag="",
        attempts=0,
        fetched_at=None,
        next_attempt_at=timezone.now(),
    )
    counts["matches_removed"] += len(matches)
    counts["resources_requeued"] += requeued
    return True


def remove_orphan_teams(seasons: tuple[Season, Season]) -> Counter[str]:
    """Remove half-season teams left without poules or fixtures.

    Native team seasons go too, but only while nothing else uses them.

    Returns:
        Counts of removed source teams, groups and native team seasons.

    """
    counts: Counter[str] = Counter()
    orphans = Team.objects.filter(season__in=seasons).exclude(
        Q(pk__in=PoolEntry.objects.values("team_id"))
        | Q(pk__in=Match.objects.values("home_team_id"))
        | Q(pk__in=Match.objects.values("away_team_id"))
        | Q(pk__in=MatchMembership.objects.values("team_id"))
    )
    for team in orphans.select_related("group"):
        with transaction.atomic():
            group = team.group
            data = {
                team.local_team_data_id,
                group.local_team_data_id if group else None,
            }
            team.delete()
            counts["teams_removed"] += 1
            if group and not Team.objects.filter(group=group).exists():
                group.delete()
                counts["groups_removed"] += 1
            for team_data in TeamData.objects.filter(pk__in=data - {None}):
                if unused(team_data):
                    team_data.delete()
                    counts["team_seasons_removed"] += 1
    return counts


def unused(team_data: TeamData) -> bool:
    """Tell whether a native team season has no people, songs or fixtures."""
    return not (
        Team.objects.filter(local_team_data=team_data).exists()
        or TeamGroup.objects.filter(local_team_data=team_data).exists()
        or team_data.players.exists()
        or team_data.coach.exists()
        or team_data.staff.exists()
        or any(
            relation.related_model.objects.filter(**{
                relation.field.name: team_data
            }).exists()
            for relation in team_data._meta.related_objects
        )
        or AppMatch.objects
        .filter(season=team_data.season)
        .filter(Q(home_team=team_data.team) | Q(away_team=team_data.team))
        .exists()
    )
