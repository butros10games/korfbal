"""Historical annotations survive adding the resumable preparation queue."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_pipeline_migration_preserves_reviewed_frames() -> None:
    """Create the queue alongside existing proposals, corrections and ownership."""
    executor = MigrationExecutor(connection)
    old_target = [("video_analysis", "0002_storedfile")]
    try:
        executor.migrate(old_target)
        old = executor.loader.project_state(old_target).apps
        owner = old.get_model("auth", "User").objects.create(
            username="migration-reviewer"
        )
        workspace = old.get_model("video_analysis", "Workspace").objects.create(
            slug="migration", owner=owner
        )
        recording = old.get_model("video_analysis", "Recording").objects.create(
            workspace=workspace, source_id="synthetic"
        )
        frame = old.get_model("video_analysis", "Frame").objects.create(
            recording=recording,
            source_id="frame",
            proposal={"objects": []},
            correction={"scene": "live", "objects": []},
            complete=True,
            status="approved",
        )
        executor = MigrationExecutor(connection)
        target = executor.loader.graph.leaf_nodes()
        executor.migrate(target)
        current = executor.loader.project_state(target).apps
        restored = current.get_model("video_analysis", "Frame").objects.get(pk=frame.pk)
        assert restored.complete
        assert restored.status == "approved"
        assert restored.correction == frame.correction
        run = current.get_model("video_analysis", "ReviewPipeline").objects.create(
            workspace_id=workspace.pk, requested_by_id=owner.pk
        )
        assert run.status == "queued"
        upload = current.get_model("video_analysis", "VideoUpload").objects.create(
            workspace_id=workspace.pk,
            requested_by_id=owner.pk,
            name="synthetic.mp4",
            size=100,
            expires_at=timezone.now(),
        )
        assert upload.received == 0
        assert upload.status == "receiving"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
