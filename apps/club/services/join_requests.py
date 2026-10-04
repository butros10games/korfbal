"""First-run account setup: club join requests, club claims and their review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.club.models import Club, ClubAdmin, ClubJoinRequest
from apps.club.services.admin import create_active_membership
from apps.player.models import Player
from apps.schedule.queries.seasons import current_season, most_recent_season
from apps.team.models import Team, TeamData


if TYPE_CHECKING:
    from django.db.models import QuerySet


MAX_CLUB_RESULTS = 20
MIN_CLUB_SEARCH_LENGTH = 2


class JoinRequestError(ValueError):
    """A setup or review step the account may not take."""


class AddToTeam(Protocol):
    """Place an approved member on the team's current-season roster."""

    def __call__(self, *, team: Team, player: Player) -> None:
        """Add the player."""
        ...


class JoinRequestEvent(Protocol):
    """Persist notification or follow-up intent for one join request."""

    def __call__(self, *, request_id: str) -> None:
        """Enqueue the work inside the caller's transaction."""
        ...


@dataclass(frozen=True)
class JoinRequestPorts:
    """Capabilities bound in `apps.club.composition`."""

    add_to_team: AddToTeam
    notify_reviewers: JoinRequestEvent
    notify_requester: JoinRequestEvent
    schedule_identity_link: JoinRequestEvent


@dataclass(frozen=True)
class KnkvMatch:
    """A visible imported player that no account has claimed yet."""

    player: Player
    club: Club | None
    team: Team | None


def _roster_teams(player: Player) -> QuerySet[TeamData]:
    return (
        TeamData.objects
        .filter(Q(players=player) | Q(coach=player) | Q(staff=player))
        .select_related("team__club", "season")
        .order_by("-season__start_date")
    )


def find_knkv_player(person_id: str) -> KnkvMatch | None:
    """Return the unclaimed, visible imported player with this KNKV person ID.

    Private and stale imports are excluded by `Player.objects`, so a private
    player always continues through the manual club/team route.
    """
    person_id = person_id.strip().upper()
    if not person_id or person_id == "PRIVATE":
        return None
    player = Player.objects.filter(
        knkv_person_id__iexact=person_id, user__isnull=True
    ).first()
    if player is None:
        return None
    latest = _roster_teams(player).first()
    team = latest.team if latest is not None else None
    club = team.club if team is not None else player.active_member_clubs().first()
    return KnkvMatch(player=player, club=club, team=team)


def search_clubs(term: str) -> list[Club]:
    """Find active clubs by name for the setup club picker."""
    term = term.strip()
    if len(term) < MIN_CLUB_SEARCH_LENGTH:
        return []
    return list(
        Club.objects.filter(dissolved=False, name__icontains=term).order_by("name")[
            :MAX_CLUB_RESULTS
        ]
    )


def club_teams(club: Club) -> list[Team]:
    """Return the club's teams in the running (or, off-season, latest) season."""
    season = current_season() or most_recent_season()
    teams = Team.objects.filter(club=club)
    if season is not None:
        teams = teams.filter(team_data__season=season)
    return list(teams.distinct().order_by("name"))


def _complete_setup(player: Player, role: str) -> None:
    player.account_role = role
    player.onboarded_at = player.onboarded_at or timezone.now()
    player.save(update_fields=("account_role", "onboarded_at"))


@transaction.atomic
def choose_spectator(player: Player) -> None:
    """Finish setup as a spectator; following clubs and teams stays optional."""
    _complete_setup(player, Player.AccountRole.SPECTATOR)


def _create(
    *,
    player: Player,
    club: Club,
    kind: str,
    ports: JoinRequestPorts,
    **fields: object,
) -> ClubJoinRequest:
    try:
        with transaction.atomic():
            request = ClubJoinRequest.objects.create(
                player=player, club=club, kind=kind, **fields
            )
    except IntegrityError as exc:
        raise JoinRequestError(
            "Je hebt al een aanmelding bij deze club die nog in behandeling is."
        ) from exc
    ports.notify_reviewers(request_id=str(request.pk))
    return request


@transaction.atomic
def request_player_membership(
    *,
    player: Player,
    club: Club,
    team: Team | None,
    knkv_person_id: str,
    ports: JoinRequestPorts,
) -> ClubJoinRequest:
    """Register as a player of a club; a club admin confirms the request.

    Raises:
        JoinRequestError: The team, KNKV identity or account state is inconsistent.

    """
    player = Player.all_objects.select_for_update().get(pk=player.pk)
    if team is not None and team.club_id != club.pk:
        raise JoinRequestError("Dit team hoort niet bij de gekozen club.")
    person_id = knkv_person_id.strip().upper()
    if person_id:
        if player.knkv_person_id:
            raise JoinRequestError("Je account is al gekoppeld aan een KNKV-speler.")
        match = find_knkv_player(person_id)
        if match is None:
            raise JoinRequestError(
                "Deze KNKV-speler is niet gevonden of al aan een account gekoppeld."
            )
        if match.club is None:
            # Linking requires a shared club; without one the claim can never be
            # applied, so the player registers with a club and team instead.
            raise JoinRequestError(
                "Dit KNKV-profiel hoort niet bij een club. Meld je aan zonder "
                "relatienummer."
            )
        if match.club.pk != club.pk:
            raise JoinRequestError("Deze KNKV-speler speelt bij een andere club.")
        person_id = str(match.player.knkv_person_id)
    request = _create(
        player=player,
        club=club,
        kind=ClubJoinRequest.Kind.MEMBER,
        ports=ports,
        team=team,
        knkv_person_id=person_id,
    )
    # Show the account's own club and team on Home while the request is reviewed.
    player.club_follow.add(club)
    if team is not None:
        player.team_follow.add(team)
    _complete_setup(player, Player.AccountRole.PLAYER)
    return request


@transaction.atomic
def request_club_admin(
    *, player: Player, club: Club, note: str, ports: JoinRequestPorts
) -> ClubJoinRequest:
    """Claim to represent a club; platform staff or a club admin verifies it.

    Raises:
        JoinRequestError: The account already administers the club.

    """
    player = Player.all_objects.select_for_update().get(pk=player.pk)
    if ClubAdmin.objects.filter(club=club, player=player).exists():
        raise JoinRequestError("Je bent al beheerder van deze club.")
    request = _create(
        player=player,
        club=club,
        kind=ClubJoinRequest.Kind.ADMIN,
        ports=ports,
        note=note.strip(),
    )
    player.club_follow.add(club)
    _complete_setup(player, Player.AccountRole.CLUB)
    return request


@transaction.atomic
def withdraw_request(*, player: Player, request_id: str) -> bool:
    """Withdraw the account's own pending request."""
    return bool(
        ClubJoinRequest.objects.filter(
            pk=request_id, player=player, status=ClubJoinRequest.Status.PENDING
        ).update(status=ClubJoinRequest.Status.WITHDRAWN, decided_at=timezone.now())
    )


def pending_member_requests(club: Club) -> list[ClubJoinRequest]:
    """List requests a club admin decides on (club claims go to platform staff)."""
    return list(
        ClubJoinRequest.objects
        .filter(
            club=club,
            kind=ClubJoinRequest.Kind.MEMBER,
            status=ClubJoinRequest.Status.PENDING,
        )
        .select_related("player__user", "team")
        .order_by("created_at")
    )


def claimed_knkv_names(requests: list[ClubJoinRequest]) -> dict[str, str]:
    """Name the imported players that requests claim, for the reviewer."""
    ids = {request.knkv_person_id for request in requests if request.knkv_person_id}
    if not ids:
        return {}
    # Only visible, unclaimed imports: a profile that turned private or stale
    # while the request waits must not reveal its name to the reviewer.
    return {
        str(person_id): name
        for person_id, name in Player.objects.filter(
            knkv_person_id__in=ids, user__isnull=True
        ).values_list("knkv_person_id", "name")
    }


@transaction.atomic
def decide_request(
    *,
    request_id: str,
    club: Club | None,
    approve: bool,
    reviewer: Player | None,
    ports: JoinRequestPorts,
) -> ClubJoinRequest:
    """Approve or reject a pending request.

    Club admins pass their `club` and may only decide member requests; platform
    staff pass `club=None` and may also decide club claims.

    Raises:
        JoinRequestError: The request is missing or already decided.

    """
    # Lock only the request: PostgreSQL cannot lock the nullable side of the
    # outer join to `team`.
    requests = ClubJoinRequest.objects.select_for_update(of=("self",)).select_related(
        "player", "club", "team"
    )
    if club is not None:
        requests = requests.filter(club=club, kind=ClubJoinRequest.Kind.MEMBER)
    request = requests.filter(pk=request_id).first()
    if request is None:
        raise JoinRequestError("Aanmelding niet gevonden.")
    if request.status != ClubJoinRequest.Status.PENDING:
        raise JoinRequestError("Over deze aanmelding is al besloten.")
    request.status = (
        ClubJoinRequest.Status.APPROVED if approve else ClubJoinRequest.Status.REJECTED
    )
    request.decided_at = timezone.now()
    request.decided_by = reviewer
    if approve:
        _apply_approval(request, ports)
    request.save()
    ports.notify_requester(request_id=str(request.pk))
    return request


def _apply_approval(request: ClubJoinRequest, ports: JoinRequestPorts) -> None:
    player = request.player
    if request.kind == ClubJoinRequest.Kind.ADMIN:
        ClubAdmin.objects.get_or_create(club=request.club, player=player)
        return
    create_active_membership(club=request.club, player=player)
    if request.knkv_person_id:
        # Linking moves the imported rosters and history onto the account; it
        # waits for the competition importer, so it runs as a background job.
        request.link_status = ClubJoinRequest.LinkStatus.PENDING
        ports.schedule_identity_link(request_id=str(request.pk))
    elif request.team is not None:
        ports.add_to_team(team=request.team, player=player)
