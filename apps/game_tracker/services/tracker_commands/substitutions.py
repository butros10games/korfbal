"""Home and opponent substitution commands."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from apps.game_tracker.domain.match_limits import MAX_SUBSTITUTIONS_PER_TEAM
from apps.game_tracker.models import MatchEvent, MatchPart, PlayerChange, PlayerGroup
from apps.game_tracker.services.event_reconciliation import (
    SubstitutionObservation,
    create_reconciliation_candidates,
    plan_substitution_reconciliation,
    record_matched_observation,
)
from apps.game_tracker.services.lineup_projections import rebuild_current_lineup
from apps.game_tracker.services.player_groups import (
    RESERVE_GROUP_NAME,
    get_reserve_group,
)
from apps.player.models import Player

from .base import (
    NO_ACTIVE_MATCH_PART_MESSAGE,
    TrackerCommandContext,
    TrackerCommandError,
    current_part,
    other_team,
)


def _part_for_substitution(context: TrackerCommandContext) -> MatchPart | None:
    if context.match_data.status != "active":
        raise TrackerCommandError("Match is not active.", code="match_not_active")

    match_part = current_part(context.match_data)
    if match_part is None and context.match_data.current_part <= 1:
        raise TrackerCommandError(
            NO_ACTIVE_MATCH_PART_MESSAGE,
            code="no_active_part",
        )
    return match_part


def _substitution_players_and_group(
    *,
    context: TrackerCommandContext,
    new_player_id: str,
    old_player_id: str,
) -> tuple[Player, Player, PlayerGroup]:
    """Resolve a reserve-for-active substitution within the reporting roster.

    Raises:
        TrackerCommandError: If either player or the lineup assignment is invalid.

    """
    try:
        player_in = Player.objects.select_related("user").get(id_uuid=new_player_id)
        player_out = Player.objects.select_related("user").get(id_uuid=old_player_id)
        active_group = PlayerGroup.objects.exclude(
            starting_type__name=RESERVE_GROUP_NAME,
        ).get(
            team=context.team,
            match_data=context.match_data,
            players__in=[player_out],
        )
        reserve_group = get_reserve_group(
            match_data=context.match_data,
            team=context.team,
        )
    except (
        Player.DoesNotExist,
        PlayerGroup.DoesNotExist,
        PlayerGroup.MultipleObjectsReturned,
        ValidationError,
        ValueError,
    ) as exc:
        raise TrackerCommandError(
            "Invalid substitution players.",
            code="bad_request",
        ) from exc

    if (
        player_in.pk == player_out.pk
        or not reserve_group.players.filter(pk=player_in.pk).exists()
    ):
        raise TrackerCommandError(
            "Incoming player is not registered as a reserve for this team.",
            code="bad_request",
        )
    return player_in, player_out, active_group


@dataclass(frozen=True, slots=True)
class GetNonActivePlayersCommand:
    """Preserve the legacy command; reserve players already ship in state."""

    def apply(self, context: TrackerCommandContext) -> None:
        """Leave state unchanged."""
        del context


@dataclass(frozen=True, slots=True)
class SubstituteCommand:
    """Replace an active player on the reporting team."""

    new_player_id: str
    old_player_id: str

    def apply(self, context: TrackerCommandContext) -> None:
        """Register the concrete substitution.

        Raises:
            TrackerCommandError: If match state or the substitution limit blocks it.

        """
        match_part = _part_for_substitution(context)
        player_in, player_out, active_group = _substitution_players_and_group(
            context=context,
            new_player_id=self.new_player_id,
            old_player_id=self.old_player_id,
        )

        plan = plan_substitution_reconciliation(
            SubstitutionObservation(
                match_data=context.match_data,
                match_part=match_part,
                reporting_team_id=context.team.pk,
                team_id=context.team.pk,
                player_out_id=player_out.pk,
                player_in_id=player_in.pk,
                effective_at=context.event_time,
            )
        )
        if plan.matched_event is not None:
            detail = plan.matched_event.substitution_detail
            if detail.player_out_id is None and detail.player_in_id is None:
                marker = PlayerChange.objects.get(
                    pk=plan.matched_event.source_id,
                    match_data=context.match_data,
                )
                marker.player_in = player_in
                marker.player_out = player_out
                marker.player_group = active_group
                marker.match_part = match_part
                marker.time = context.event_time
                marker.save(
                    update_fields=[
                        "player_in",
                        "player_out",
                        "player_group",
                        "match_part",
                        "time",
                    ]
                )
            else:
                record_matched_observation(
                    event=plan.matched_event,
                    effective_at=context.event_time,
                    payload={
                        "kind": "player_change",
                        "team_id": str(context.team.pk),
                        "player_out_id": str(player_out.pk),
                        "player_in_id": str(player_in.pk),
                    },
                )
            rebuild_current_lineup(context.match_data)
            return

        substitutions = PlayerChange.objects.filter(
            match_data=context.match_data,
            player_group__team=context.team,
        ).count()
        if substitutions >= MAX_SUBSTITUTIONS_PER_TEAM:
            raise TrackerCommandError(
                "Max wissels bereikt.",
                code="max_substitutions",
            )

        change = PlayerChange.objects.create(
            player_in=player_in,
            player_out=player_out,
            player_group=active_group,
            match_data=context.match_data,
            match_part=match_part,
            time=context.event_time,
        )
        event = MatchEvent.objects.get(
            source_type="player_change",
            source_id=change.pk,
        )
        create_reconciliation_candidates(
            event=event,
            possible_duplicates=plan.review_events,
        )
        rebuild_current_lineup(context.match_data)


@dataclass(frozen=True, slots=True)
class OpponentSubstitutionCommand:
    """Register an opponent substitution without player identities."""

    def apply(self, context: TrackerCommandContext) -> None:
        """Register the anonymous opponent substitution.

        Raises:
            TrackerCommandError: If match state or the substitution limit blocks it.

        """
        match_part = _part_for_substitution(context)
        opponent = other_team(context.match, context.team)
        reserve_group = get_reserve_group(
            match_data=context.match_data,
            team=opponent,
        )
        plan = plan_substitution_reconciliation(
            SubstitutionObservation(
                match_data=context.match_data,
                match_part=match_part,
                reporting_team_id=context.team.pk,
                team_id=opponent.pk,
                player_out_id=None,
                player_in_id=None,
                effective_at=context.event_time,
            )
        )
        if plan.matched_event is not None:
            record_matched_observation(
                event=plan.matched_event,
                effective_at=context.event_time,
                payload={
                    "kind": "player_change",
                    "team_id": str(opponent.pk),
                    "player_out_id": None,
                    "player_in_id": None,
                },
            )
            return

        substitutions = PlayerChange.objects.filter(
            match_data=context.match_data,
            player_group__team=opponent,
        ).count()
        if substitutions >= MAX_SUBSTITUTIONS_PER_TEAM:
            raise TrackerCommandError(
                "Max wissels bereikt.",
                code="max_substitutions",
            )

        change = PlayerChange.objects.create(
            player_in=None,
            player_out=None,
            player_group=reserve_group,
            match_data=context.match_data,
            match_part=match_part,
            time=context.event_time,
        )
        event = MatchEvent.objects.get(
            source_type="player_change",
            source_id=change.pk,
        )
        create_reconciliation_candidates(
            event=event,
            possible_duplicates=plan.review_events,
        )
