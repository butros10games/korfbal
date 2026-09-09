"""Issue and validate narrowly scoped, expiring tracker capabilities."""

from datetime import timedelta
from hashlib import sha256
import secrets

from django.db.models import QuerySet
from django.utils import timezone

from apps.game_tracker.models import TrackerAccessLink
from apps.schedule.models import Match
from apps.team.models import Team


SESSION_KEY = "tracker_access"


def token_digest(token: str) -> str:
    """Hash a high-entropy invitation secret before storage."""
    return sha256(token.encode()).hexdigest()


def issue_tracker_link(match: Match, team: Team) -> tuple[str, TrackerAccessLink]:
    """Replace this team's previous invitation and all sessions using it."""
    token = secrets.token_urlsafe(32)
    link, _ = TrackerAccessLink.objects.update_or_create(
        match=match,
        team=team,
        defaults={
            "token_hash": token_digest(token),
            "expires_at": timezone.now() + timedelta(days=7),
        },
    )
    return token, link


def active_tracker_links(match: Match, team: Team) -> QuerySet[TrackerAccessLink]:
    """Check expiry on every request, including already redeemed invitations."""
    return TrackerAccessLink.objects.filter(
        match=match, team=team, expires_at__gt=timezone.now()
    )
