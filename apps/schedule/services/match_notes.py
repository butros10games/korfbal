"""Team-scoped note access and author-owned changes."""

from dataclasses import dataclass
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from apps.schedule.models import Match, MatchNote
from apps.team.models import TeamData


class NoteAccessDeniedError(Exception):
    """The viewer is outside the selected team's season roster."""


class NoteConflictError(Exception):
    """Another edit changed the version the author was working on."""


class NoteNotFoundError(Exception):
    """The note is outside this match/team or is no longer present."""


@dataclass(frozen=True)
class NoteChange:
    """The intended update; a null text requests deletion."""

    expected_revision: int
    text: str | None


def require_team_access(*, match: Match, team_id: UUID, user_id: int) -> None:
    """Require season roster membership.

    Raises:
        NoteAccessDeniedError: The viewer is not a member of this match team.

    """
    if team_id not in {match.home_team_id, match.away_team_id}:
        raise NoteAccessDeniedError
    if (
        not TeamData.objects
        .filter(team_id=team_id, season_id=match.season_id)
        .filter(
            Q(players__user_id=user_id)
            | Q(coach__user_id=user_id)
            | Q(staff__user_id=user_id)
        )
        .exists()
    ):
        raise NoteAccessDeniedError


@transaction.atomic
def change_note(
    *,
    match: Match,
    team_id: UUID,
    user_id: int,
    note_id: UUID,
    change: NoteChange,
) -> MatchNote | None:
    """Recheck membership and lock this note.

    Raises:
        NoteNotFoundError: The note does not exist in this scope.
        NoteAccessDeniedError: The viewer is not the author or a member.
        NoteConflictError: The submitted revision is stale.

    """
    require_team_access(match=match, team_id=team_id, user_id=user_id)
    note = (
        MatchNote.objects
        .select_for_update()
        .filter(pk=note_id, match=match, team_id=team_id)
        .first()
    )
    if note is None:
        raise NoteNotFoundError
    if note.author_id != user_id:
        raise NoteAccessDeniedError
    if note.revision != change.expected_revision:
        raise NoteConflictError
    if change.text is None:
        note.delete()
        return None
    note.text = change.text
    note.revision += 1
    note.save(update_fields=["text", "revision", "updated_at"])
    return note
