"""Explicitly reviewed links from provider identities to existing accounts."""

from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.models import (
    MatchMembership,
    RosterMembership,
    SyncLease,
    SyncResource,
)
from apps.competition.services.player_photos import PREFIX, photo_name
from apps.competition.services.rosters import ROSTER_RELATIONS
from apps.player.models import Player
from apps.team.models import TeamData, TeamRosterMembership


def _validate_source(source: Player, target: Player) -> None:
    if source.user_id or not target.user_id:
        raise ValueError("The source must be an import and the target an account")
    if target.knkv_person_id:
        raise ValueError("The account already has a different KNKV identity")
    if not Player.objects.filter(pk=source.pk).exists():
        raise ValueError("The imported identity is private or stale")
    _validate_native_history(source)
    if (
        RosterMembership.objects.filter(player=target).exists()
        or MatchMembership.objects.filter(player=target).exists()
    ):
        raise ValueError("Account already has provider observations")
    if SyncResource.objects.filter(
        kind="player_photo", source_id=str(target.pk)
    ).exists():
        raise ValueError("Account already has a provider photo checkpoint")
    source_clubs = TeamData.objects.filter(
        Q(players=source) | Q(staff=source) | Q(coach=source)
    ).values("team__club_id")
    same_club = (
        TeamData.objects.filter(
            Q(players=target) | Q(staff=target) | Q(coach=target),
            team__club_id__in=source_clubs,
        ).exists()
        or target.member_clubs.filter(pk__in=source_clubs).exists()
    )
    if not same_club:
        raise ValueError("The profiles have no shared club membership")


def _validate_native_history(source: Player) -> None:
    allowed = {RosterMembership, MatchMembership, TeamData, TeamRosterMembership}
    for relation in Player._meta.related_objects:
        if relation.related_model in allowed:
            continue
        if relation.related_model._base_manager.filter(**{
            relation.field.name: source
        }).exists():
            raise ValueError(
                "Imported profile has native history in "
                f"{relation.related_model._meta.label}"
            )
    for field in Player._meta.many_to_many:
        if getattr(source, field.name).exists():
            raise ValueError("Imported profile has native follows or memberships")


def _move_rosters(source: Player, target: Player) -> None:
    for relation, flag in ROSTER_RELATIONS.items():
        through = getattr(TeamData, relation).through
        present = set(
            through.objects.filter(player=target).values_list("teamdata_id", flat=True)
        )
        source_data = set(
            through.objects.filter(player=source).values_list("teamdata_id", flat=True)
        )
        # A pre-existing native link must never become importer-owned: privacy
        # withdrawals and roster departures may only remove links we created.
        RosterMembership.objects.filter(
            player=source, published_team_data_id__in=present
        ).update(**{flag: False})
        through.objects.bulk_create(
            [
                through(player_id=target.pk, teamdata_id=pk)
                for pk in source_data - present
            ],
            ignore_conflicts=True,
        )
        through.objects.filter(player=source).delete()
    now = timezone.now()
    for row in TeamRosterMembership.objects.filter(player=source, ended_at=None):
        if TeamRosterMembership.objects.filter(
            player=target, team_data_id=row.team_data_id, role=row.role, ended_at=None
        ).exists():
            TeamRosterMembership.objects.filter(pk=row.pk).update(ended_at=now)
    TeamRosterMembership.objects.filter(player=source).update(player=target)
    RosterMembership.objects.filter(player=source).update(player=target)
    MatchMembership.objects.filter(player=source).update(player=target)


def _move_photo(source: Player, target: Player) -> None:
    old_name = source.profile_picture.name or ""
    if old_name and not old_name.startswith(PREFIX):
        raise ValueError("Imported profile has a native uploaded photo")
    target.knkv_photo = source.knkv_photo if not target.profile_picture else ""
    resources = SyncResource.objects.filter(
        kind="player_photo", source_id=str(source.pk)
    )
    if target.profile_picture:
        resources.delete()
    else:
        resources.update(source_id=str(target.pk))
        if old_name and target.knkv_photo:
            storage = source.profile_picture.storage
            destination = photo_name(target)
            if not storage.exists(destination):
                with storage.open(old_name, "rb") as content:
                    destination = storage.save(destination, content)
            target.profile_picture = destination
    if old_name:
        storage = source.profile_picture.storage
        transaction.on_commit(lambda: storage.delete(old_name), robust=True)


def _validate_links(
    links: list[dict[str, str]], *, apply: bool
) -> tuple[list[str], list[str]]:
    if any(set(row) != {"knkv_person_id", "account_player_id"} for row in links):
        raise ValueError("Each link requires knkv_person_id and account_player_id")
    identities = [row["knkv_person_id"] for row in links]
    accounts = [str(UUID(row["account_player_id"])) for row in links]
    if len(set(identities)) != len(links) or len(set(accounts)) != len(links):
        raise ValueError("Each identity and account may appear only once")
    if apply:
        lease, _ = SyncLease.objects.get_or_create(
            key="sportlink", defaults={"expires_at": timezone.now()}
        )
        lease = SyncLease.objects.select_for_update().get(pk=lease.pk)
        if lease.expires_at > timezone.now():
            raise ValueError(
                "Stop the competition importer before applying player links"
            )
    return identities, accounts


@transaction.atomic
def link_players(links: list[dict[str, str]], *, apply: bool = False) -> list[dict]:
    """Preview or atomically apply explicit pairs; never merge based on similarity.

    Raises:
        ValueError: Pairs are ambiguous, an import is running, or history needs review.

    """
    identities, accounts = _validate_links(links, apply=apply)
    people = Player.all_objects.select_related("user")
    if apply:
        # Lock only Player: the optional joined User relation is nullable.
        people = people.select_for_update(of=("self",))
    results = []
    pairs = []
    for identity, account in zip(identities, accounts, strict=True):
        source = people.get(knkv_person_id=identity)
        target = people.get(pk=account)
        if source.pk == target.pk:
            results.append({"account_player_id": account, "status": "already_linked"})
            continue
        _validate_source(source, target)
        if source.profile_picture and not source.profile_picture.name.startswith(
            PREFIX
        ):
            raise ValueError("Imported profile has a native uploaded photo")
        results.append({
            "account_player_id": account,
            "imported_player_id": str(source.pk),
            "status": "linked" if apply else "ready",
            "rosters": source.knkv_memberships.count(),
            "match_selections": MatchMembership.objects.filter(player=source).count(),
        })
        pairs.append((source, target))
    if apply:
        for source, target in pairs:
            identity = source.knkv_person_id
            target.knkv_person_id = identity
            target.knkv_privacy = source.knkv_privacy
            target.knkv_observed_at = source.knkv_observed_at
            _move_rosters(source, target)
            _move_photo(source, target)
            # Retain the account UUID, all native relations and profile preferences.
            # Transfer the unique provider key so future fetches find this player.
            source.delete()
            target.save(
                update_fields=(
                    "knkv_person_id",
                    "knkv_privacy",
                    "knkv_observed_at",
                    "knkv_photo",
                    "profile_picture",
                )
            )
    return results
