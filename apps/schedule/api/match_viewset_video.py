"""Match video: public playback of a published recording, synced to its periods."""

from __future__ import annotations

from rest_framework import permissions, serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import (
    APIException,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.request import Request
from rest_framework.response import Response

from apps.video_analysis.composition import match_video_urls
from apps.video_analysis.services import match_video

from .match_viewset_contracts import MatchViewSetContext
from .permissions import IsCoachOrAdmin


class MatchVideoPartSerializer(serializers.Serializer):
    """One tracked period and where it starts in the video."""

    match_part_id = serializers.UUIDField()
    part_number = serializers.IntegerField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField(allow_null=True)
    video_seconds = serializers.FloatField(allow_null=True)


class MatchVideoSerializer(serializers.Serializer):
    """A playable recording and its period sync."""

    url = serializers.URLField(allow_null=True)
    duration_seconds = serializers.FloatField()
    published = serializers.BooleanField()
    revision = serializers.IntegerField()
    parts = MatchVideoPartSerializer(many=True)


class MatchVideoResponseSerializer(serializers.Serializer):
    """The video visible to this viewer, if any, and whether they may edit it."""

    video = MatchVideoSerializer(allow_null=True)
    can_edit = serializers.BooleanField()


class MatchVideoUpdateSerializer(serializers.Serializer):
    """Publish or sync the match video; omitted fields keep their values."""

    expected_revision = serializers.IntegerField(min_value=0)
    published = serializers.BooleanField(required=False)
    anchors = serializers.DictField(
        child=serializers.FloatField(allow_null=True), required=False
    )


class MatchVideoConflictApiError(APIException):
    """Structured HTTP 409 so editors keep their draft and reload."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "revision_conflict"

    def __init__(self, conflict: match_video.MatchVideoConflictError) -> None:
        """Expose both revisions without internal details."""
        Exception.__init__(self, str(conflict))
        self.detail = {
            "code": self.default_code,
            "detail": str(conflict),
            "expected_revision": conflict.expected_revision,
            "revision": conflict.revision,
        }


class MatchVideoActionsMixin:
    """Read the match video publicly; publish and sync it as an event editor."""

    @action(
        detail=True,
        methods=("GET", "PUT"),
        url_path="video",
        permission_classes=[permissions.AllowAny],
    )
    def video(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return or update the match video for this viewer.

        Raises:
            NotAuthenticated: An anonymous viewer tried to update.
            PermissionDenied: The viewer may not edit this match.
            NotFound: The match has no stored video to update.
            MatchVideoConflictApiError: Another editor saved first.
            ValidationError: The sync point is invalid.

        """
        del args, kwargs
        match = self.get_object()
        can_edit = IsCoachOrAdmin().has_permission(request, self)
        if request.method == "PUT":
            if not can_edit:
                if not request.user.is_authenticated:
                    raise NotAuthenticated
                raise PermissionDenied(IsCoachOrAdmin.message)
            payload = MatchVideoUpdateSerializer(data=request.data)
            payload.is_valid(raise_exception=True)
            data = payload.validated_data
            try:
                match_video.update(
                    match,
                    request.user,
                    match_video.MatchVideoUpdate(
                        expected_revision=data["expected_revision"],
                        published=data.get("published"),
                        anchors=data.get("anchors"),
                    ),
                )
            except LookupError as error:
                raise NotFound(str(error)) from error
            except match_video.MatchVideoConflictError as conflict:
                raise MatchVideoConflictApiError(conflict) from conflict
            except ValueError as error:
                raise ValidationError({"anchors": [str(error)]}) from error
        response = Response(
            match_video.read(match, can_edit=can_edit, urls=match_video_urls())
        )
        # Signed URLs and editor state are per viewer: never share a cached copy.
        response["Cache-Control"] = "private, no-store"
        return response
