"""Django persistence adapter for the portable review engine."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from apps.video_analysis.engine.storage_workspace import reserve_space
from apps.video_analysis.engine.store import Store
from apps.video_analysis.file_storage import WorkspaceFiles
from apps.video_analysis.models import Frame, Recording, ReviewAudit, Workspace
from apps.video_analysis.queries import frame_payload


class DatabaseStore(Store):
    """Reuse annotation validation with PostgreSQL locks instead of a JSON writer."""

    def __init__(
        self,
        workspace: Workspace,
        actor: User | None = None,
        files: WorkspaceFiles | None = None,
    ) -> None:
        """Bind one immutable workspace storage root and audit actor."""
        self.workspace_id = workspace.pk
        self.actor = actor
        self.files = files
        self.root = (
            Path(settings.VIDEO_ANALYSIS_ROOT).resolve()
            / str(workspace.owner_id)
            / str(workspace.pk)
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "review.json"

    def recording(self, match_id: str) -> dict[str, Any]:
        """Load one video's metadata without materializing the frame catalogue.

        Raises:
            ValueError: The recording is outside this workspace.

        """
        row = (
            Recording.objects
            .filter(workspace_id=self.workspace_id, source_id=match_id)
            .values("metadata")
            .first()
        )
        if row is None:
            raise ValueError("Unknown recording")
        return {**row["metadata"], "id": match_id}

    def publish_media(self, relative: str) -> None:
        """Publish a new image before its review row becomes visible."""
        if self.files:
            self.files.publish_media(relative)

    @contextmanager
    def video_source(self, relative: str) -> Iterator[str]:
        """Keep the private range reader alive for the entire decode operation.

        Yields:
            A loopback object reader, or a local source for offline workspaces.

        """
        if self.files:
            with self.files.video_source(relative) as source:
                yield source
        else:
            yield str(self.media(relative))

    def media(self, relative: str) -> Path:
        """Fetch missing worker inputs from the authoritative private bucket."""
        return (
            self.files.cache_media(relative) if self.files else super().media(relative)
        )

    def media_size(self, relative: str) -> int:
        """Read object length without filling the web server's cache."""
        return self.files.size(relative) if self.files else super().media_size(relative)

    def media_chunks(self, relative: str, start: int, end: int) -> Iterator[bytes]:
        """Stream directly from private object storage when configured.

        Yields:
            Bounded response chunks.

        """
        yield from (
            self.files.chunks(relative, start, end)
            if self.files
            else super().media_chunks(relative, start, end)
        )

    def sync_artifacts(self) -> None:
        """Publish worker outputs before marking jobs complete."""
        if self.files:
            self.files.sync_artifacts()

    def reserve_working_bytes(self, additional: int) -> None:
        """Reserve space for generated data as well as downloaded inputs."""
        if self.files:
            reserve_space(
                self.root,
                additional,
                settings.VIDEO_ANALYSIS_WORKSPACE_MAX_BYTES,
                settings.VIDEO_ANALYSIS_PIPELINE_WORK_FREE_BYTES,
            )

    def artifact_location(self, relative: str) -> dict | None:
        """Resolve a published artifact for direct controller/worker transfer."""
        return self.files.artifact_location(relative) if self.files else None

    def publish_artifact(self, relative: str) -> None:
        """Keep a small interactive metadata change independent of model archives."""
        if self.files:
            self.files.publish_artifact(relative)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize changes while checking the selected frame's own version."""
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=self.workspace_id)
            yield

    def read(self) -> dict[str, Any]:
        """Project normalized rows into the stable engine contract."""
        # One joined statement observes a consistent workspace revision and frame
        # snapshot without serializing readers behind long-running worker writes.
        matches: dict[str, dict[str, Any]] = {}
        revision = None
        fields = (
            "source_id",
            "metadata",
            "proposal",
            "correction",
            "history",
            "status",
            "complete",
        )
        rows = (
            Recording.objects
            .filter(workspace_id=self.workspace_id)
            .order_by("position", "pk", "frames__position", "frames__pk")
            .values(
                "source_id",
                "metadata",
                "workspace__revision",
                *(f"frames__{key}" for key in fields),
            )
        )
        for row in rows:
            revision = row["workspace__revision"]
            match = matches.setdefault(
                row["source_id"], dict(row["metadata"], id=row["source_id"], frames=[])
            )
            if row["frames__source_id"] is None:
                continue
            frame = Frame(**{key: row[f"frames__{key}"] for key in fields})
            if frame.metadata.get("dataset_decision") != "removed":
                match["frames"].append(frame_payload(frame))
        if revision is None:
            revision = Workspace.objects.get(pk=self.workspace_id).revision
        return {
            "schema_version": 1,
            "revision": revision,
            "matches": list(matches.values()),
        }

    def _persist(self, data: dict[str, Any]) -> None:
        """Persist only changed rows; never delete omitted recordings or reviews."""
        workspace = Workspace.objects.get(pk=self.workspace_id)
        workspace.revision += 1
        workspace.save(update_fields=["revision"])
        data["revision"] = workspace.revision
        for position, match in enumerate(data["matches"]):
            metadata = {k: v for k, v in match.items() if k not in {"id", "frames"}}
            recording, _ = Recording.objects.get_or_create(
                workspace=workspace,
                source_id=match["id"],
                defaults={"metadata": metadata, "position": position},
            )
            if recording.metadata != metadata:
                recording.metadata = metadata
                recording.save(update_fields=["metadata"])
            existing = {f.source_id: f for f in recording.frames.all()}
            for index, raw in enumerate(match["frames"]):
                values = {
                    "position": index,
                    "metadata": {
                        k: v
                        for k, v in raw.items()
                        if k
                        not in {
                            "id",
                            "proposal",
                            "correction",
                            "history",
                            "status",
                            "complete",
                        }
                    },
                    "proposal": raw.get("proposal"),
                    "correction": raw.get("correction"),
                    "history": raw.get("history", []),
                    "status": raw["status"],
                    "complete": raw.get("complete", False),
                }
                frame = existing.get(raw["id"])
                if frame is None:
                    Frame.objects.create(
                        recording=recording, source_id=raw["id"], **values
                    )
                else:
                    self._update_frame(frame, values, workspace.revision)
        if self.files:
            self.files.publish_review(self.read())

    def _update_frame(
        self, frame: Frame, values: dict[str, Any], revision: int
    ) -> None:
        """Preserve triage decisions when older background work saves annotations."""
        if frame.metadata.get("dataset_decision") == "removed":
            return
        for key in (
            "dataset_decision",
            "dataset_revision",
            "curation",
            "label_check",
            "blind_check",
        ):
            if key in frame.metadata:
                values["metadata"][key] = frame.metadata[key]
        if not any(getattr(frame, key) != value for key, value in values.items()):
            return
        if (
            frame.correction != values["correction"]
            or frame.status != values["status"]
            or frame.complete != values["complete"]
        ):
            ReviewAudit.objects.create(
                frame=frame,
                actor=self.actor,
                revision=revision,
                payload=values,
            )
        for key, value in values.items():
            setattr(frame, key, value)
        frame.save()
