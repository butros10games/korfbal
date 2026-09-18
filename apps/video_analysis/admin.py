"""Connect source recordings to canonical Korfbal matches through protected admin."""

from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from apps.video_analysis.models import AnalysisJob, Recording, Workspace


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    """Only the canonical match link is editable; imported identities stay fixed."""

    list_display = ("source_id", "workspace", "match")
    readonly_fields = ("workspace", "source_id", "metadata", "position")
    raw_id_fields = ("match",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Create recordings through validated import commands."""
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Model | None = None
    ) -> bool:
        """Keep reviewed footage and its provenance intact."""
        return False


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    """Inspect migration and ownership without changing storage identities."""

    readonly_fields = ("id", "slug", "owner", "revision", "source_digest", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Workspaces are created by verified import commands."""
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Model | None = None
    ) -> bool:
        """Keep authoritative review work and artifact ownership intact."""
        return False


@admin.register(AnalysisJob)
class AnalysisJobAdmin(admin.ModelAdmin):
    """Inspect durable requests without directly editing worker state."""

    list_display = ("id", "kind", "status", "created_at", "finished_at")
    readonly_fields = (
        "id",
        "workspace",
        "requested_by",
        "kind",
        "payload",
        "status",
        "message",
        "created_at",
        "finished_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Use validated application endpoints to request work."""
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Model | None = None
    ) -> bool:
        """Retain execution history."""
        return False
