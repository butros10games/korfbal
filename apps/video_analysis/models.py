"""Authoritative review records; model output and human edits remain separate."""

from typing import ClassVar
import uuid

from django.conf import settings
from django.db import models


class Workspace(models.Model):
    """Private staff-operated workspace with an immutable storage identity."""

    objects: ClassVar[models.Manager["Workspace"]] = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    owner_id: int
    revision = models.PositiveBigIntegerField(default=0)
    source_digest = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return a safe operational label."""
        return self.slug


class Recording(models.Model):
    """A source recording optionally linked to the canonical sporting match."""

    objects: ClassVar[models.Manager["Recording"]] = models.Manager()

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="recordings"
    )
    frames: models.Manager["Frame"]
    source_id = models.CharField(max_length=100)
    match = models.ForeignKey(
        "schedule.Match", null=True, blank=True, on_delete=models.SET_NULL
    )
    metadata = models.JSONField(default=dict)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        """Keep imported identities unique and ordering stable."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["workspace", "source_id"], name="video_recording_source_unique"
            )
        ]
        ordering: ClassVar = ["position", "pk"]

    def __str__(self) -> str:
        """Return a safe operational label."""
        return self.source_id


class Frame(models.Model):
    """One sampled frame and its current review, retaining the original proposal."""

    objects: ClassVar[models.Manager["Frame"]] = models.Manager()

    recording = models.ForeignKey(
        Recording, on_delete=models.CASCADE, related_name="frames"
    )
    source_id = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict)
    proposal = models.JSONField(null=True)
    correction = models.JSONField(null=True)
    history = models.JSONField(default=list)
    status = models.CharField(max_length=16, default="pending")
    complete = models.BooleanField(default=False)

    class Meta:
        """Keep imported identities unique and ordering stable."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["recording", "source_id"], name="video_frame_source_unique"
            )
        ]
        ordering: ClassVar = ["position", "pk"]

    def __str__(self) -> str:
        """Return a safe operational label."""
        return self.source_id


class ReviewAudit(models.Model):
    """Append-only attribution for native human review changes."""

    objects: ClassVar[models.Manager["ReviewAudit"]] = models.Manager()

    frame_id: int
    frame = models.ForeignKey(Frame, on_delete=models.PROTECT)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    revision = models.PositiveBigIntegerField()
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return a safe operational label."""
        return f"{self.frame_id}: {self.revision}"


class AnalysisJob(models.Model):
    """Durable work request; execution belongs to background workers."""

    objects: ClassVar[models.Manager["AnalysisJob"]] = models.Manager()
    workspace_id: uuid.UUID
    requested_by_id: int | None

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    kind = models.CharField(max_length=24)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default="queued")
    message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    def __str__(self) -> str:
        """Return a safe operational label."""
        return f"{self.kind}: {self.status}"


class StoredFile(models.Model):
    """Verified private object behind a workspace-relative logical filename."""

    objects: ClassVar[models.Manager["StoredFile"]] = models.Manager()
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    relative_path = models.CharField(max_length=1000)
    bucket = models.CharField(max_length=63)
    object_key = models.CharField(max_length=1000)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    source_mtime_ns = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep each logical path scoped to its workspace."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["workspace", "relative_path"], name="video_stored_path_unique"
            )
        ]

    def __str__(self) -> str:
        """Return the logical filename without exposing credentials."""
        return self.relative_path


class ReviewPipeline(models.Model):
    """Frozen data-preparation recipe and resumable progress, separate from labels."""

    objects: ClassVar[models.Manager["ReviewPipeline"]] = models.Manager()
    workspace_id: uuid.UUID
    requested_by_id: int | None

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    recipe = models.JSONField(default=dict)
    progress = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default="queued")
    message = models.CharField(max_length=300, blank=True)
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        """Identify the preparation without exposing private source URLs."""
        return f"{self.pk}: {self.status}"


class ClipReview(models.Model):
    """Human inspection of an immutable tracking run; never a training annotation."""

    objects: ClassVar[models.Manager["ClipReview"]] = models.Manager()
    job_id: uuid.UUID
    pipeline_id: uuid.UUID

    pipeline = models.ForeignKey(ReviewPipeline, on_delete=models.PROTECT)
    job = models.OneToOneField(AnalysisJob, on_delete=models.PROTECT)
    status = models.CharField(max_length=24, default="pending")
    notes = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=0)
    history = models.JSONField(default=list)

    def __str__(self) -> str:
        """Identify the immutable replay under review."""
        return f"{self.job_id}: {self.status}"


class VideoUpload(models.Model):
    """Owner-scoped, expiring chunk intake before a recording is trusted."""

    objects: ClassVar[models.Manager["VideoUpload"]] = models.Manager()
    workspace_id: uuid.UUID
    requested_by_id: int | None

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField()
    received = models.PositiveBigIntegerField(default=0)
    parts = models.JSONField(default=list)
    status = models.CharField(max_length=24, default="receiving")
    expires_at = models.DateTimeField()

    def __str__(self) -> str:
        """Identify intake without disclosing the uploaded filename."""
        return f"{self.pk}: {self.status}"
