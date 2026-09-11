"""Synthetic handler imported only by the isolated worker integration test."""

from time import sleep

from celery import shared_task
from django.db.models import F

from apps.kwt_common.models import BackgroundJob


@shared_task(ignore_result=True)
def probe(marker_id: int) -> None:
    """Pause before the side effect so the test can terminate an executing worker."""
    marker = BackgroundJob.objects.get(pk=marker_id)
    if marker.error == "hold":
        BackgroundJob.objects.filter(pk=marker_id).update(error="entered")
        while BackgroundJob.objects.filter(pk=marker_id, error="entered").exists():
            sleep(0.1)
    BackgroundJob.objects.filter(pk=marker_id).update(
        completed_generation=F("completed_generation") + 1
    )
