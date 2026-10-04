"""Celery adapters for club join requests."""

from __future__ import annotations

from datetime import timedelta
import logging

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.club.models import ClubAdmin, ClubJoinRequest
from apps.competition.models import SyncLease
from apps.competition.services.player_linking import link_players
from apps.kwt_common.models import BackgroundJob
from apps.kwt_common.services.jobs import enqueue
from apps.player.composition import send_push_to_users
from apps.player.services.web_push import WebPushPayload


logger = logging.getLogger(__name__)

LINK_RETRY_DELAY = timedelta(minutes=2)
# Give up waiting for a quiet moment in the importer after this long.
LINK_DEADLINE = timedelta(days=2)
IMPORTER_BUSY = "De KNKV-import was te lang bezig; koppel de speler handmatig."


def _support_address() -> str:
    return str(
        getattr(settings, "KORFBAL_SUPPORT_EMAIL", "")
        or getattr(settings, "BG_AUTH_SUPPORT_EMAIL", "")
    )


def _requester_name(request: ClubJoinRequest) -> str:
    return request.player.display_name


def _load(request_id: str) -> ClubJoinRequest | None:
    return (
        ClubJoinRequest.objects
        .select_related("player__user", "club", "team")
        .filter(pk=request_id)
        .first()
    )


@shared_task(ignore_result=True)
def notify_join_request_reviewers(*, request_id: str) -> None:
    """Tell the club admins (or, for club claims, the platform team) to review."""
    request = _load(request_id)
    if request is None or request.status != ClubJoinRequest.Status.PENDING:
        return
    origin = settings.WEB_APP_ORIGIN
    name = _requester_name(request)
    if request.kind == ClubJoinRequest.Kind.MEMBER:
        admins = list(
            ClubAdmin.objects.filter(
                club=request.club, player__user__is_active=True
            ).values_list("player__user_id", "player__user__email")
        )
        team = f" ({request.team.name})" if request.team else ""
        body = f"{name} wil als speler lid worden van {request.club.name}{team}."
        url = f"/clubs/{request.club.pk}?tab=settings"
    else:
        admins = []
        body = f"{name} wil {request.club.name} beheren."
        url = f"/clubs/{request.club.pk}"
    if admins:
        send_push_to_users(
            user_ids=[user_id for user_id, _ in admins],
            payload=WebPushPayload(
                title="Nieuwe aanmelding",
                body=body,
                url=url,
                tag=f"club-join-request:{request.pk}",
            ),
        )
    # Clubs without an admin yet, and every club claim, go to the platform team.
    recipients = [email for _, email in admins if email] or [_support_address()]
    recipients = [address for address in recipients if address]
    if not recipients:
        logger.error("Join request %s has no reviewer to notify", request.pk)
        return
    lines = [body]
    if request.knkv_person_id:
        lines.append(f"Opgegeven KNKV-relatienummer: {request.knkv_person_id}")
    if request.note:
        lines.append(f"Toelichting: {request.note}")
    lines += [
        "",
        f"Bekijk de aanmelding: {origin}{url}"
        if admins
        else "Beoordeel de aanmelding in het beheer: "
        + reverse("admin:club_clubjoinrequest_changelist"),
    ]
    send_mail(
        subject=f"[KorfConnect] Aanmelding voor {request.club.name}",
        message="\n".join(lines),
        from_email=None,
        recipient_list=recipients,
    )


@shared_task(ignore_result=True)
def notify_join_request_decision(*, request_id: str) -> None:
    """Tell the requester whether the club accepted them."""
    request = _load(request_id)
    if request is None or request.player.user is None:
        return
    approved = request.status == ClubJoinRequest.Status.APPROVED
    if not approved and request.status != ClubJoinRequest.Status.REJECTED:
        return
    role = "beheerder" if request.kind == ClubJoinRequest.Kind.ADMIN else "lid"
    title = "Aanmelding geaccepteerd" if approved else "Aanmelding afgewezen"
    body = (
        f"Je bent nu {role} van {request.club.name}."
        if approved
        else f"{request.club.name} heeft je aanmelding niet geaccepteerd."
    )
    url = f"/clubs/{request.club.pk}"
    send_push_to_users(
        user_ids=[request.player.user.pk],
        payload=WebPushPayload(
            title=title, body=body, url=url, tag=f"club-join-decision:{request.pk}"
        ),
    )
    if request.player.user.email:
        send_mail(
            subject=f"[KorfConnect] {title}",
            message=f"{body}\n\n{settings.WEB_APP_ORIGIN}{url}",
            from_email=None,
            recipient_list=[request.player.user.email],
        )


def _importer_running() -> bool:
    return SyncLease.objects.filter(
        key="sportlink", expires_at__gt=timezone.now()
    ).exists()


def _retry_link(request_id: str) -> None:
    enqueue(
        "apps.club.tasks.link_join_request_identity",
        request_id,
        kwargs={"request_id": request_id},
        due_at=timezone.now() + LINK_RETRY_DELAY,
    )


@shared_task(ignore_result=True)
def link_join_request_identity(*, request_id: str) -> None:
    """Move the claimed KNKV player's history onto the approved account.

    The linking service refuses to run during a competition import, so busy
    attempts and unexpected errors are rescheduled until the importer pauses or
    the deadline passes; the request always ends linked or failed.
    """
    request = _load(request_id)
    if (
        request is None
        or request.status != ClubJoinRequest.Status.APPROVED
        or request.link_status != ClubJoinRequest.LinkStatus.PENDING
    ):
        return
    try:
        _link(request)
    except Exception as exc:
        # Unexpected errors (storage, database) may be transient. Retry on our own
        # bounded schedule: the job envelope stops after a few attempts and would
        # otherwise leave the request pending forever.
        logger.exception("Linking join request %s failed", request.pk)
        _schedule_or_fail(request, f"Koppelen mislukt ({type(exc).__name__}).")


def _link(request: ClubJoinRequest) -> None:
    links = [
        {
            "knkv_person_id": request.knkv_person_id,
            "account_player_id": str(request.player_id),
        }
    ]
    if _importer_running():
        _schedule_or_fail(request, IMPORTER_BUSY)
        return
    try:
        with transaction.atomic():
            link_players(links, apply=True)
            ClubJoinRequest.objects.filter(pk=request.pk).update(
                link_status=ClubJoinRequest.LinkStatus.LINKED, link_error=""
            )
    except (ValueError, ObjectDoesNotExist) as exc:
        # The importer may have started between the check and the link.
        if _importer_running():
            _schedule_or_fail(request, IMPORTER_BUSY)
            return
        ClubJoinRequest.objects.filter(pk=request.pk).update(
            link_status=ClubJoinRequest.LinkStatus.FAILED, link_error=str(exc)[:200]
        )


def _schedule_or_fail(request: ClubJoinRequest, reason: str) -> None:
    decided = request.decided_at or request.created_at
    if timezone.now() - decided < LINK_DEADLINE:
        _retry_link(str(request.pk))
        return
    ClubJoinRequest.objects.filter(pk=request.pk).update(
        link_status=ClubJoinRequest.LinkStatus.FAILED, link_error=reason
    )


@shared_task(ignore_result=True)
def resume_stalled_join_request_links() -> None:
    """Re-queue approved links whose job stopped without settling the request.

    A job can exhaust its attempts while the database is unavailable; the next
    run then retries or, after the deadline, marks the link failed.
    """
    pending = ClubJoinRequest.objects.filter(
        status=ClubJoinRequest.Status.APPROVED,
        link_status=ClubJoinRequest.LinkStatus.PENDING,
    )
    for request in pending:
        key = f"apps.club.tasks.link_join_request_identity:{request.pk}"
        if BackgroundJob.objects.filter(
            Q(due_at__isnull=False) | Q(next_due_at__isnull=False), key=key
        ).exists():
            continue
        _schedule_or_fail(request, "Koppelen is gestopt; koppel de speler handmatig.")
