"""Wake committed private form work without a periodic queue polling delay."""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.competition.models import (
    Match as SourceMatch,
    MatchFormAccess,
    MatchFormSync,
)
from apps.competition.services.match_form_worker import FORM_RESOURCES, discover
from apps.competition.tasks import sync_match_forms
from apps.game_tracker.models import MatchLiveChange


DISPATCH_HORIZON_SECONDS = 60


@receiver(post_save, sender=MatchFormSync)
def wake_form_job(sender: type, instance: MatchFormSync, **kwargs: object) -> None:
    """Publish pending intents only after commit; periodic recovery covers outages."""
    if instance.state != "pending":
        return
    delay = max(0, (instance.next_attempt_at - timezone.now()).total_seconds())
    if delay <= DISPATCH_HORIZON_SECONDS:
        transaction.on_commit(
            lambda: sync_match_forms.apply_async(countdown=delay, expires=300),
            robust=True,
        )


@receiver(post_save, sender=MatchLiveChange)
def queue_match_form_change(
    sender: type,
    instance: MatchLiveChange,
    created: bool,
    **kwargs: object,
) -> None:
    """Record form work with the final revision, inside the match transaction."""
    if created and FORM_RESOURCES.intersection(instance.resources):
        discover(match_id=instance.match_data.match_link_id)


@receiver(post_save, sender=MatchFormAccess)
def queue_connected_team(
    sender: type,
    instance: MatchFormAccess,
    **kwargs: object,
) -> None:
    """Catch up eligible work when a team is connected or enabled."""
    if instance.enabled:
        discover(access_id=instance.pk)


@receiver(post_save, sender=SourceMatch)
def queue_published_fixture(
    sender: type,
    instance: SourceMatch,
    update_fields: frozenset[str] | None = None,
    **kwargs: object,
) -> None:
    """Catch up imports made due by a fixture link or schedule change."""
    relevant = {"local_match", "starts_at", "pool", "home_team", "away_team"}
    if instance.local_match_id and (
        update_fields is None or relevant.intersection(update_fields)
    ):
        discover(match_id=instance.local_match_id)
