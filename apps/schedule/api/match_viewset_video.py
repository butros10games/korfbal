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

from apps.kwt_common.api.params import UUID_URL_REGEX
from apps.video_analysis.composition import match_video_urls
from apps.video_analysis.services import (
    match_video,
    match_video_annotations,
    match_video_playlists,
)

from .match_viewset_contracts import MatchViewSetContext
from .permissions import IsCoachOrAdmin


class MatchVideoPartSerializer(serializers.Serializer):
    """One tracked period and where it starts in the video."""

    match_part_id = serializers.UUIDField()
    part_number = serializers.IntegerField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField(allow_null=True)
    video_seconds = serializers.FloatField(allow_null=True)


class MatchVideoBreakSerializer(serializers.Serializer):
    """Where the recording skips real time within a period, and by how much."""

    video_seconds = serializers.FloatField()
    skipped_seconds = serializers.FloatField()


class MatchVideoWhistleSerializer(serializers.Serializer):
    """A detected whistle, exposed only to editors."""

    seconds = serializers.FloatField()
    duration = serializers.FloatField()
    strength = serializers.FloatField()


class MatchVideoSerializer(serializers.Serializer):
    """A playable recording and its period sync."""

    url = serializers.URLField(allow_null=True)
    duration_seconds = serializers.FloatField()
    published = serializers.BooleanField()
    revision = serializers.IntegerField()
    parts = MatchVideoPartSerializer(many=True)
    breaks = MatchVideoBreakSerializer(many=True)
    whistles = MatchVideoWhistleSerializer(many=True, required=False)
    whistles_status = serializers.CharField(required=False, allow_blank=True)


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
    breaks = MatchVideoBreakSerializer(many=True, required=False)


class MatchVideoConflictApiError(APIException):
    """Structured HTTP 409 so editors keep their draft and reload."""

    detail: dict[str, str | int]
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
            ValidationError: A sync point or recording break is invalid.

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
                        breaks=None
                        if data.get("breaks") is None
                        else [
                            (row["video_seconds"], row["skipped_seconds"])
                            for row in data["breaks"]
                        ],
                    ),
                )
            except LookupError as error:
                raise NotFound(str(error)) from error
            except match_video.MatchVideoConflictError as conflict:
                raise MatchVideoConflictApiError(conflict) from conflict
            except match_video.MatchVideoBreakError as error:
                raise ValidationError({"breaks": [str(error)]}) from error
            except ValueError as error:
                raise ValidationError({"anchors": [str(error)]}) from error
        response = Response(
            match_video.read(match, can_edit=can_edit, urls=match_video_urls())
        )
        # Signed URLs and editor state are per viewer: never share a cached copy.
        response["Cache-Control"] = "private, no-store"
        return response

    @action(
        detail=True,
        methods=("POST",),
        url_path="video/whistles",
        permission_classes=[IsCoachOrAdmin],
    )
    def video_whistles(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Start a search for referee whistles in the video's sound.

        Raises:
            NotFound: The match has no stored video.

        """
        del args, kwargs
        match = self.get_object()
        try:
            match_video.request_whistles(match)
        except LookupError as error:
            raise NotFound(str(error)) from error
        response = Response(
            match_video.read(match, can_edit=True, urls=match_video_urls()),
            status=status.HTTP_202_ACCEPTED,
        )
        response["Cache-Control"] = "private, no-store"
        return response


class MatchVideoAnnotationSerializer(serializers.Serializer):
    """A tag, note or clip on the match video."""

    id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=sorted(match_video_annotations.KINDS))
    label = serializers.CharField(allow_blank=True)
    body = serializers.CharField(allow_blank=True)
    start_seconds = serializers.FloatField()
    end_seconds = serializers.FloatField(allow_null=True)
    player_ids = serializers.ListField(child=serializers.UUIDField())
    visibility = serializers.ChoiceField(
        choices=sorted(match_video_annotations.VISIBILITIES)
    )
    drawing = serializers.JSONField(allow_null=True)
    author = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()


class MatchVideoAnnotationsSerializer(serializers.Serializer):
    """The annotations this viewer may see, and whether they may add some."""

    annotations = MatchVideoAnnotationSerializer(many=True)
    can_edit = serializers.BooleanField()


class MatchVideoAnnotationInputSerializer(serializers.Serializer):
    """Writable annotation fields; PATCH keeps omitted values."""

    kind = serializers.ChoiceField(
        choices=sorted(match_video_annotations.KINDS), required=False
    )
    label = serializers.CharField(max_length=80, allow_blank=True, required=False)
    body = serializers.CharField(max_length=2000, allow_blank=True, required=False)
    start_seconds = serializers.FloatField(min_value=0, required=False)
    end_seconds = serializers.FloatField(min_value=0, allow_null=True, required=False)
    player_ids = serializers.ListField(
        child=serializers.UUIDField(), max_length=16, required=False
    )
    visibility = serializers.ChoiceField(
        choices=sorted(match_video_annotations.VISIBILITIES), required=False
    )
    drawing = serializers.JSONField(allow_null=True, required=False)


class MatchVideoAnnotationResponseSerializer(serializers.Serializer):
    """An annotation returned after a write."""

    annotation = MatchVideoAnnotationSerializer()


def _annotation_input(request: Request) -> dict[str, object]:
    """Return the request body as a mapping.

    Raises:
        ValidationError: The body is not a JSON object.

    """
    if not isinstance(request.data, dict):
        raise ValidationError({"detail": ["Send the annotation as an object."]})
    return dict(request.data)


class MatchVideoAnnotationActionsMixin:
    """Read the video's annotations; editors add, change and remove them."""

    @action(
        detail=True,
        methods=("GET", "POST"),
        url_path="video/annotations",
        permission_classes=[permissions.AllowAny],
    )
    def video_annotations(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """List the visible annotations, or add one as an editor.

        Raises:
            NotAuthenticated: An anonymous viewer tried to add one.
            PermissionDenied: The viewer may not edit this match.
            NotFound: The match has no stored video.
            ValidationError: The annotation is invalid.

        """
        del args, kwargs
        match = self.get_object()
        can_edit = IsCoachOrAdmin().has_permission(request, self)
        if request.method == "POST":
            if not can_edit:
                if not request.user.is_authenticated:
                    raise NotAuthenticated
                raise PermissionDenied(IsCoachOrAdmin.message)
            try:
                created = match_video_annotations.create(
                    match, request.user, _annotation_input(request)
                )
            except LookupError as error:
                raise NotFound(str(error)) from error
            except match_video_annotations.AnnotationError as error:
                raise ValidationError({"detail": [str(error)]}) from error
            return Response({"annotation": created}, status=status.HTTP_201_CREATED)
        response = Response({
            "annotations": match_video_annotations.list_annotations(
                match, can_edit=can_edit
            ),
            "can_edit": can_edit,
        })
        response["Cache-Control"] = "private, no-store"
        return response

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=rf"video/annotations/(?P<annotation_id>{UUID_URL_REGEX})",
        permission_classes=[IsCoachOrAdmin],
    )
    def video_annotation_detail(
        self: MatchViewSetContext,
        request: Request,
        annotation_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Change or remove one annotation.

        Raises:
            NotFound: The annotation does not exist on this match's video.
            ValidationError: The change is invalid.

        """
        del args, kwargs
        match = self.get_object()
        try:
            if request.method == "DELETE":
                match_video_annotations.delete(match, annotation_id)
                return Response(status=status.HTTP_204_NO_CONTENT)
            updated = match_video_annotations.update(
                match, annotation_id, _annotation_input(request)
            )
        except LookupError as error:
            raise NotFound(str(error)) from error
        except match_video_annotations.AnnotationError as error:
            raise ValidationError({"detail": [str(error)]}) from error
        return Response({"annotation": updated})


class MatchVideoPlaylistSerializer(serializers.Serializer):
    """A named, ordered set of saved clips."""

    id = serializers.UUIDField()
    title = serializers.CharField()
    annotation_ids = serializers.ListField(child=serializers.UUIDField())
    visibility = serializers.ChoiceField(
        choices=sorted(match_video_annotations.VISIBILITIES)
    )
    author = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()


class MatchVideoPlaylistsSerializer(serializers.Serializer):
    """The playlists this viewer may see."""

    playlists = MatchVideoPlaylistSerializer(many=True)
    can_edit = serializers.BooleanField()


class MatchVideoPlaylistInputSerializer(serializers.Serializer):
    """Writable playlist fields; PATCH keeps omitted values."""

    title = serializers.CharField(max_length=80, required=False)
    annotation_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=100, required=False
    )
    visibility = serializers.ChoiceField(
        choices=sorted(match_video_annotations.VISIBILITIES), required=False
    )


class MatchVideoPlaylistResponseSerializer(serializers.Serializer):
    """A playlist returned after a write."""

    playlist = MatchVideoPlaylistSerializer()


class MatchVideoPlaylistActionsMixin:
    """Read the video's playlists; editors add, change and remove them."""

    @action(
        detail=True,
        methods=("GET", "POST"),
        url_path="video/playlists",
        permission_classes=[permissions.AllowAny],
    )
    def video_playlists(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """List the visible playlists, or add one as an editor.

        Raises:
            NotAuthenticated: An anonymous viewer tried to add one.
            PermissionDenied: The viewer may not edit this match.
            NotFound: The match has no stored video.
            ValidationError: The playlist is invalid.

        """
        del args, kwargs
        match = self.get_object()
        can_edit = IsCoachOrAdmin().has_permission(request, self)
        if request.method == "POST":
            if not can_edit:
                if not request.user.is_authenticated:
                    raise NotAuthenticated
                raise PermissionDenied(IsCoachOrAdmin.message)
            try:
                created = match_video_playlists.create(
                    match, request.user, _annotation_input(request)
                )
            except LookupError as error:
                raise NotFound(str(error)) from error
            except match_video_annotations.AnnotationError as error:
                raise ValidationError({"detail": [str(error)]}) from error
            return Response({"playlist": created}, status=status.HTTP_201_CREATED)
        response = Response({
            "playlists": match_video_playlists.list_playlists(match, can_edit=can_edit),
            "can_edit": can_edit,
        })
        response["Cache-Control"] = "private, no-store"
        return response

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=rf"video/playlists/(?P<playlist_id>{UUID_URL_REGEX})",
        permission_classes=[IsCoachOrAdmin],
    )
    def video_playlist_detail(
        self: MatchViewSetContext,
        request: Request,
        playlist_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Change or remove one playlist.

        Raises:
            NotFound: The playlist does not exist on this match's video.
            ValidationError: The change is invalid.

        """
        del args, kwargs
        match = self.get_object()
        try:
            if request.method == "DELETE":
                match_video_playlists.delete(match, playlist_id)
                return Response(status=status.HTTP_204_NO_CONTENT)
            updated = match_video_playlists.update(
                match, playlist_id, _annotation_input(request)
            )
        except LookupError as error:
            raise NotFound(str(error)) from error
        except match_video_annotations.AnnotationError as error:
            raise ValidationError({"detail": [str(error)]}) from error
        return Response({"playlist": updated})
