"""Service helpers for match MVP voting.

This module was originally located in `apps.schedule.services.mvp`.
It now lives in `apps.awards` so MVP can evolve into notifications/badges
without bloating the scheduling domain.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from apps.awards.models.mvp import MatchMvp, MatchMvpVote
from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    MatchPlayer,
    PlayerChange,
    Shot,
)
from apps.player.models.player import Player
from apps.schedule.models.match import Match


# Keep the MVP voting window short so the result is available quickly.
VOTING_WINDOW = timedelta(hours=3)


@dataclass(frozen=True)
class MvpCandidate:
    """Serializable MVP candidate payload for the frontend."""

    id_uuid: str
    username: str
    display_name: str
    profile_picture_url: str | None
    team_side: str | None


def _finished_at_from_match_data(match_data: MatchData) -> datetime:
    end = (
        MatchPart.objects
        .filter(match_data=match_data)
        .aggregate(max_end=Max("end_time"))
        .get("max_end")
    )
    if isinstance(end, datetime):
        return end

    # Fallback: best-effort from last known tracker events.
    last_shot = (
        Shot.objects
        .filter(match_data=match_data)
        .aggregate(max_time=Max("time"))
        .get("max_time")
    )
    if isinstance(last_shot, datetime):
        return last_shot

    last_change = (
        PlayerChange.objects
        .filter(match_data=match_data)
        .aggregate(max_time=Max("time"))
        .get("max_time")
    )
    if isinstance(last_change, datetime):
        return last_change

    return timezone.now()


def get_or_create_match_mvp(match: Match, match_data: MatchData) -> MatchMvp:
    """Ensure there is a MatchMvp record for a finished match."""
    finished_at = _finished_at_from_match_data(match_data)
    if timezone.is_naive(finished_at):
        finished_at = timezone.make_aware(finished_at, timezone.get_current_timezone())

    closes_at = finished_at + VOTING_WINDOW

    obj, created = MatchMvp.objects.get_or_create(
        match=match,
        defaults={
            "finished_at": finished_at,
            "closes_at": closes_at,
        },
    )

    # If we created it, great. If it existed but was missing timestamps (older
    # data), keep it consistent.
    if not created:
        needs_update = False
        if not obj.finished_at:
            obj.finished_at = finished_at
            needs_update = True
        if not obj.closes_at:
            obj.closes_at = closes_at
            needs_update = True

        # If the voting window duration changed, shorten any *still-open*
        # windows that haven't been published yet.
        if obj.finished_at and obj.closes_at and obj.published_at is None:
            desired_closes_at = obj.finished_at + VOTING_WINDOW
            if obj.closes_at > desired_closes_at and timezone.now() < obj.closes_at:
                obj.closes_at = desired_closes_at
                needs_update = True

        if needs_update:
            obj.save(update_fields=["finished_at", "closes_at", "updated_at"])

    return obj


def build_mvp_candidates(match: Match, match_data: MatchData) -> list[MvpCandidate]:
    """Return a stable list of players that can be voted MVP for this match."""
    home_id = str(match.home_team.id_uuid)
    away_id = str(match.away_team.id_uuid)

    def side_from_team_id(team_id: str | None) -> str | None:
        if team_id == home_id:
            return "home"
        if team_id == away_id:
            return "away"
        return None

    def candidate_from_player(player: Player, *, team_id: str | None) -> MvpCandidate:
        username = player.user.username
        display_name = player.user.get_full_name() or username
        return MvpCandidate(
            id_uuid=str(player.id_uuid),
            username=username,
            display_name=display_name,
            profile_picture_url=player.get_profile_picture(),
            team_side=side_from_team_id(team_id),
        )

    def add_candidate(
        acc: dict[str, MvpCandidate],
        *,
        player: Player | None,
        team_id: str | None,
    ) -> None:
        if player is None:
            return
        pid = str(player.id_uuid)
        if pid in acc:
            return
        acc[pid] = candidate_from_player(player, team_id=team_id)

    candidates: dict[str, MvpCandidate] = {}

    match_players = MatchPlayer.objects.select_related("player__user", "team").filter(
        match_data=match_data,
    )
    for mp in match_players:
        add_candidate(
            candidates,
            player=mp.player,
            team_id=str(mp.team_id) if mp.team_id else None,
        )

    # Fallback if match roster wasn't registered.
    if not candidates:
        _add_candidates_from_events(match_data, candidates)

    return _sort_candidates(candidates.values())


def _add_candidates_from_events(
    match_data: MatchData,
    acc: dict[str, MvpCandidate],
) -> None:
    """Fallback candidate extraction from events."""

    def safe_add(player: Player | None) -> None:
        if player is None:
            return
        pid = str(player.id_uuid)
        if pid in acc:
            return
        username = player.user.username
        display_name = player.user.get_full_name() or username
        acc[pid] = MvpCandidate(
            id_uuid=pid,
            username=username,
            display_name=display_name,
            profile_picture_url=player.get_profile_picture(),
            team_side=None,
        )

    for shot in Shot.objects.select_related("player__user", "team").filter(
        match_data=match_data,
    ):
        safe_add(shot.player)

    for change in PlayerChange.objects.select_related(
        "player_in__user",
        "player_out__user",
        "player_group__team",
    ).filter(match_data=match_data):
        safe_add(change.player_in)
        safe_add(change.player_out)


def _sort_candidates(candidates: Iterable[MvpCandidate]) -> list[MvpCandidate]:
    """Stable ordering: home first then away then username."""

    def sort_key(c: MvpCandidate) -> tuple[int, str]:
        bucket = 2
        if c.team_side == "home":
            bucket = 0
        elif c.team_side == "away":
            bucket = 1
        return (bucket, c.username.lower())

    return sorted(candidates, key=sort_key)


def _candidate_is_valid(
    *,
    candidate: Player,
    candidates: Iterable[MvpCandidate],
) -> bool:
    cid = str(candidate.id_uuid)
    return any(c.id_uuid == cid for c in candidates)


def _validate_vote_or_raise(
    *,
    match: Match,
    match_data: MatchData,
    candidate: Player,
) -> None:
    mvp = get_or_create_match_mvp(match, match_data)
    now = timezone.now()

    if now >= mvp.closes_at:
        raise ValueError("Voting is closed.")

    candidates = build_mvp_candidates(match, match_data)
    if not _candidate_is_valid(candidate=candidate, candidates=candidates):
        raise ValueError("Invalid MVP candidate.")


@transaction.atomic
def ensure_mvp_published(match: Match, match_data: MatchData) -> MatchMvp:
    """If voting is closed and not published yet, compute and persist the winner."""
    mvp = get_or_create_match_mvp(match, match_data)

    # Already published.
    if mvp.mvp_player_id and mvp.published_at:
        return mvp

    now = timezone.now()
    if now < mvp.closes_at:
        return mvp

    # Select winner by vote count. Tie-breaker: lowest UUID string (stable).
    winner_row = (
        MatchMvpVote.objects
        .filter(match=match)
        .values("candidate")
        .annotate(votes=Count("id_uuid"))
        .order_by("-votes", "candidate")
        .first()
    )

    if not winner_row:
        # No votes cast; mark closed but unpublished winner.
        mvp.published_at = now
        mvp.save(update_fields=["published_at", "updated_at"])
        return mvp

    winner_id = winner_row.get("candidate")
    winner = Player.objects.filter(id_uuid=winner_id).first() if winner_id else None
    if not winner:
        mvp.published_at = now
        mvp.save(update_fields=["published_at", "updated_at"])
        return mvp

    mvp.mvp_player = winner
    mvp.published_at = now
    mvp.save(update_fields=["mvp_player", "published_at", "updated_at"])
    return mvp


@transaction.atomic
def cast_vote(
    *,
    match: Match,
    match_data: MatchData,
    voter: Player,
    candidate: Player,
) -> MatchMvpVote:
    """Create or update a voter's MVP vote."""
    _validate_vote_or_raise(match=match, match_data=match_data, candidate=candidate)

    obj, _created = MatchMvpVote.objects.update_or_create(
        match=match,
        voter=voter,
        defaults={"candidate": candidate},
    )
    return obj


@transaction.atomic
def cast_vote_anon(
    *,
    match: Match,
    match_data: MatchData,
    voter_token: str,
    candidate: Player,
) -> MatchMvpVote:
    """Create or update an anonymous MVP vote tied to a cookie token."""
    _validate_vote_or_raise(match=match, match_data=match_data, candidate=candidate)

    obj, _created = MatchMvpVote.objects.update_or_create(
        match=match,
        voter_token=voter_token,
        defaults={"candidate": candidate},
    )
    return obj
