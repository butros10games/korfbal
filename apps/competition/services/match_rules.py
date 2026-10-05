"""Resolve a published fixture's rule profile and hand it to the tracker."""

from __future__ import annotations

from django.db.models import F, Q

from apps.competition.domain.match_rules import RuleContext, resolve_rules
from apps.competition.models import Match
from apps.competition.services.match_details import component_state
from apps.game_tracker.domain.match_rules import MatchRules
from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_rule_profiles import apply_rule_profile
from apps.schedule.queries.seasons import season_edition


SPORT_DISCIPLINES = {"KORFBALL-ZA-WK": "indoor", "KORFBALL-VE-WK": "outdoor"}
# Classes the classifier could not settle do not select rules.
UNSETTLED = {"conflict", "unresolved"}


def rule_context(source: Match) -> RuleContext:
    """Build the context from the edition and the poule's official class."""
    discipline = SPORT_DISCIPLINES.get(source.home_team.sport)
    pool = source.pool
    if (
        pool is None
        or pool.competition_class_id is None
        or pool.mapping_status in UNSETTLED
    ):
        return RuleContext(edition=season_edition(source.season), discipline=discipline)
    row = pool.competition_class
    assert row is not None
    return RuleContext(
        edition=season_edition(source.season),
        discipline=row.edition.discipline
        if row.edition.discipline != "unknown"
        else discipline,
        category=row.category,
        age_group=row.age_group,
        colour=row.colour,
        playing_format=row.playing_format,
        code=row.code,
        gender=row.edition.gender,
        team_kind=row.team_kind,
    )


def source_rules(source: Match) -> MatchRules:
    """Resolve the profile; validated official periods win over class rules."""
    timing_available = (
        source.playing_time_observed_at is not None
        and component_state(source, "match_timing") == "available"
    )
    return resolve_rules(
        rule_context(source),
        minutes=source.playing_time_minutes if timing_available else None,
        periods=(source.match_periods or ()) if timing_available else (),
    )


def sync_tracker_rules(source: Match) -> str | None:
    """Apply the source profile to its native tracker under a row lock.

    Returns:
        The tracker outcome, or None for an unpublished fixture.

    """
    if source.local_match_id is None:
        return None
    compatible_pool = Q(pool__local_pool_id=F("local_match__pool_id")) | Q(
        pool__isnull=True, local_match__pool__isnull=True
    )
    if not Match.objects.filter(
        compatible_pool,
        pk=source.pk,
        local_match__home_team_id=F("home_team__group__local_team_id"),
        local_match__away_team_id=F("away_team__group__local_team_id"),
    ).exists():
        return None
    tracker = MatchData.objects.select_for_update(no_key=True).get(
        match_link_id=source.local_match_id
    )
    return apply_rule_profile(tracker, source_rules(source))


RULE_RELATIONS = (
    "season",
    "home_team",
    "pool__competition_class__edition",
)
