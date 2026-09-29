"""Production entrypoints bind video-analysis services to the real adapters."""

import uuid

import pytest

from apps.video_analysis import composition
from apps.video_analysis.models import Workspace


def test_pipeline_runtime_binds_storage_and_isolated_vision_capabilities() -> None:
    """Advancing a review pipeline uses leased storage and the vision runtime."""
    runtime = composition.pipeline_runtime()

    assert runtime.processing_store is composition.processing_store
    assert runtime.has_capacity is composition.pipeline_has_capacity
    assert runtime.import_source is composition.import_pipeline_source
    assert runtime.infer_batch is composition.infer_pipeline_batch
    assert runtime.extract_frames is composition.extract_pipeline_frames
    assert runtime.run_clip is composition.run_clip


def test_upload_purge_uses_an_unhydrated_store_for_the_upload_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload cleanup resolves the owning workspace's store, not a caller path."""
    stores: list[tuple[object, object, bool]] = []
    purged: list[tuple[object, uuid.UUID]] = []
    workspace, upload_id, store = Workspace(), uuid.uuid4(), object()

    def worker_store(owner: object, user: object, *, hydrate: bool) -> object:
        stores.append((owner, user, hydrate))
        return store

    monkeypatch.setattr(composition, "worker_store", worker_store)
    monkeypatch.setattr(
        composition,
        "purge_uploaded_chunks",
        lambda target, key: purged.append((target, key)),
    )
    composition.purge_upload(workspace, upload_id)

    assert stores == [(workspace, None, False)]
    assert purged == [(store, upload_id)]
