"""Authenticated JSON endpoints for team-private match notes."""

from typing import cast
from uuid import UUID

from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.schedule.models import Match, MatchNote
from apps.schedule.services.match_notes import (
    NoteAccessDeniedError,
    NoteChange,
    NoteConflictError,
    NoteNotFoundError,
    change_note,
    require_team_access,
)


class NoteInput(serializers.Serializer):
    """Notes are plain text with a bounded size."""

    text = serializers.CharField(max_length=4000)


class NoteEditInput(NoteInput):
    """Authors must submit the version they started editing."""

    expected_revision = serializers.IntegerField(min_value=1)


class NoteDeleteInput(serializers.Serializer):
    """Deletion also protects newer edits."""

    expected_revision = serializers.IntegerField(min_value=1)


class TeamInput(serializers.Serializer):
    """A selected match team is mandatory for every operation."""

    team = serializers.UUIDField()


class Conflict(APIException):
    """A structured conflict preserves the client's pending text."""

    status_code = status.HTTP_409_CONFLICT

    def __init__(self) -> None:
        """Return a stable error code alongside a readable message."""
        super().__init__({
            "code": "revision_conflict",
            "detail": "This note changed. Reload before saving.",
        })


class NoteSerializer(serializers.ModelSerializer):
    """Expose author labels, never private account identifiers."""

    author_name = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()

    def get_author_name(self, obj: MatchNote) -> str:
        """Display a former author's note without requiring their account."""
        return (
            (obj.author.get_full_name() or obj.author.username)
            if obj.author
            else "Verwijderde gebruiker"
        )

    def get_can_edit(self, obj: MatchNote) -> bool:
        """Only the author may modify a note."""
        return obj.author_id == self.context["request"].user.pk

    class Meta:
        """Keep content scoped to the authenticated response."""

        model = MatchNote
        fields = (
            "id",
            "text",
            "revision",
            "created_at",
            "updated_at",
            "author_name",
            "can_edit",
        )


class NotePageSerializer(serializers.Serializer):
    """The bounded list response including navigation links."""

    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = NoteSerializer(many=True)


_TEAM_PARAMETER = OpenApiParameter(
    "team",
    OpenApiTypes.UUID,
    OpenApiParameter.QUERY,
    required=True,
    description="Match team whose season roster can access these notes.",
)


@extend_schema(parameters=[_TEAM_PARAMETER])
class MatchNotesView(APIView):
    """List/add notes or edit/delete a specific author-owned note."""

    permission_classes = (permissions.IsAuthenticated,)

    def dispatch(
        self, request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        """Prevent shared/browser caches from retaining private notes or denials."""
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Cookie, Authorization, Accept"
        return response

    def scope(self, request: Request, match_id: UUID) -> tuple[Match, UUID]:
        """Authorize the selected team.

        Raises:
            PermissionDenied: The viewer is outside the roster.

        """
        values = TeamInput(data=request.query_params)
        values.is_valid(raise_exception=True)
        match = get_object_or_404(Match, pk=match_id)
        team_id = values.validated_data["team"]
        try:
            require_team_access(
                match=match, team_id=team_id, user_id=cast(int, request.user.pk)
            )
        except NoteAccessDeniedError as error:
            raise PermissionDenied(
                "Only this team's season roster and staff can access notes."
            ) from error
        return match, team_id

    @extend_schema(
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter("page_size", OpenApiTypes.INT, OpenApiParameter.QUERY),
        ],
        responses=NotePageSerializer,
    )
    def get(self, request: Request, match_id: UUID) -> Response:
        """Read one bounded page for the selected team."""
        match, team_id = self.scope(request, match_id)
        rows = (
            MatchNote.objects
            .filter(match=match, team_id=team_id)
            .select_related("author")
            .order_by("-created_at", "-pk")
        )
        pager = StandardResultsSetPagination()
        page = pager.paginate_queryset(rows, request, view=self)
        return pager.get_paginated_response(
            NoteSerializer(page, many=True, context={"request": request}).data
        )

    @extend_schema(request=NoteInput, responses={201: NoteSerializer})
    def post(self, request: Request, match_id: UUID) -> Response:
        """Add an observation without altering match tracking."""
        match, team_id = self.scope(request, match_id)
        values = NoteInput(data=request.data)
        values.is_valid(raise_exception=True)
        note = MatchNote.objects.create(
            match=match,
            team_id=team_id,
            author=request.user,
            text=values.validated_data["text"],
        )
        return Response(
            NoteSerializer(note, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class MatchNoteDetailView(MatchNotesView):
    """Only update/delete methods are exposed for individual notes."""

    def __init__(self, **kwargs: object) -> None:
        """Limit detail routes to author mutations."""
        super().__init__(**kwargs)
        self.http_method_names = ["patch", "delete", "options"]

    def update(
        self, request: Request, match_id: UUID, note_id: UUID, *, delete: bool
    ) -> Response:
        """Translate service errors.

        Raises:
            PermissionDenied: The viewer cannot modify this note.
            Conflict: The submitted revision is stale.
            NotFound: No note exists in this scope.

        """
        match, team_id = self.scope(request, match_id)
        values = (
            NoteDeleteInput(data=request.data)
            if delete
            else NoteEditInput(data=request.data)
        )
        values.is_valid(raise_exception=True)
        try:
            note = change_note(
                match=match,
                team_id=team_id,
                user_id=cast(int, request.user.pk),
                note_id=note_id,
                change=NoteChange(
                    expected_revision=values.validated_data["expected_revision"],
                    text=None if delete else values.validated_data["text"],
                ),
            )
        except NoteAccessDeniedError as error:
            raise PermissionDenied("Only the author can change this note.") from error
        except NoteConflictError as error:
            raise Conflict from error
        except NoteNotFoundError as error:
            raise NotFound from error
        return (
            Response(status=status.HTTP_204_NO_CONTENT)
            if delete
            else Response(NoteSerializer(note, context={"request": request}).data)
        )

    @extend_schema(
        request=NoteEditInput, responses={200: NoteSerializer, 409: OpenApiTypes.OBJECT}
    )
    def patch(self, request: Request, match_id: UUID, note_id: UUID) -> Response:
        """Edit an existing note."""
        return self.update(request, match_id, note_id, delete=False)

    @extend_schema(
        request=NoteDeleteInput, responses={204: None, 409: OpenApiTypes.OBJECT}
    )
    def delete(self, request: Request, match_id: UUID, note_id: UUID) -> Response:
        """Delete the version confirmed by its author."""
        return self.update(request, match_id, note_id, delete=True)
