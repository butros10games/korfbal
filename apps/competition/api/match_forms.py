"""Private status and durable commands for a connected account's match forms."""

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.competition.application.match_forms import MatchFormError, MatchFormOptions
from apps.competition.models import MatchFormAccess, MatchFormSync
from apps.competition.services.match_forms import (
    allows_substitutions,
    enqueue,
    resolve_scope,
)
from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_mutations import MatchRevisionConflictError


_FORM_ERROR_DETAILS = {
    "not_connected": "No connected KNKV match form is available for this match.",
    "access_denied": "You do not have permission to edit this team's match form.",
    "team_not_linked": "This team is not linked to the KNKV match form.",
    "invalid_action": "The requested match-form action or options are invalid.",
    "match_started": "The match has started; lineup changes are closed.",
    "substitutions_not_enabled": "Substitution submission is not enabled.",
    "captain_required": "Select a captain before publishing the lineup.",
    "captain_not_selected": "The selected captain must be in the match lineup.",
    "players_not_linked": "Link all selected players to KNKV before publishing.",
}
_FORM_ERROR_STATUSES = {
    "not_connected": status.HTTP_404_NOT_FOUND,
    "access_denied": status.HTTP_403_FORBIDDEN,
    "invalid_action": status.HTTP_400_BAD_REQUEST,
    "captain_required": status.HTTP_400_BAD_REQUEST,
    "captain_not_selected": status.HTTP_400_BAD_REQUEST,
}


class MatchFormCommandSerializer(serializers.Serializer):
    """Require the revision of the lineup the user intends to publish/import."""

    action = serializers.ChoiceField(choices=("import", "publish", "substitutions"))
    expected_revision = serializers.IntegerField(min_value=0)
    captain_player_id = serializers.UUIDField(required=False)


class MatchFormJobSerializer(serializers.ModelSerializer):
    """Expose bounded receipts without upstream bodies or session details."""

    captain_player_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        """Return receipts only, never connection or upstream data."""

        model = MatchFormSync
        fields = (
            "action",
            "state",
            "error_code",
            "updated_at",
            "player_count",
            "event_count",
            "captain_player_id",
        )


class MatchFormStatusSerializer(serializers.Serializer):
    """Describe the local permissions and latest action receipts."""

    connected = serializers.BooleanField()
    can_import = serializers.BooleanField(required=False)
    can_publish = serializers.BooleanField(required=False)
    auto_substitutions = serializers.BooleanField(required=False)
    can_send_substitutions = serializers.BooleanField(required=False)
    jobs = MatchFormJobSerializer(many=True, required=False)


class MatchFormView(APIView):
    """Only the explicitly bound account can use its private KNKV session."""

    permission_classes = (permissions.IsAuthenticated,)

    def finalize_response(
        self, request: Request, response: Response, *args: object, **kwargs: object
    ) -> Response:
        """Keep account-specific form state out of browser and intermediary caches."""
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response

    def _access(self, request: Request, team_id: str) -> MatchFormAccess | None:
        return (
            MatchFormAccess.objects
            .select_related("user", "team")
            .filter(user=request.user, team_id=team_id, enabled=True)
            .first()
        )

    @extend_schema(responses=MatchFormStatusSerializer)
    def get(self, request: Request, match_id: str, team_id: str) -> Response:
        """Return only the requesting account's connection and action status."""
        access = self._access(request, team_id)
        if access is None:
            return Response({"connected": False})
        try:
            source, tracker, _ = resolve_scope(access, match_id)
        except (MatchFormError, MatchData.DoesNotExist):
            return Response({"connected": False})
        return Response({
            "connected": True,
            "can_import": tracker.status == "upcoming",
            "can_publish": tracker.status == "upcoming",
            "auto_substitutions": allows_substitutions(access, source),
            "can_send_substitutions": tracker.status == "finished"
            and allows_substitutions(access, source),
            "jobs": MatchFormJobSerializer(
                MatchFormSync.objects.filter(access=access, match_id=match_id),
                many=True,
            ).data,
        })

    @extend_schema(
        request=MatchFormCommandSerializer, responses={202: MatchFormJobSerializer}
    )
    def post(self, request: Request, match_id: str, team_id: str) -> Response:
        """Queue a revision-checked action; the browser never receives credentials."""
        access = self._access(request, team_id)
        if access is None:
            return Response(
                {
                    "code": "not_connected",
                    "detail": "Connect your KNKV account to this team first.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = MatchFormCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = serializer.validated_data
            job = enqueue(
                access,
                match_id,
                data["action"],
                data["expected_revision"],
                options=MatchFormOptions(
                    captain_player_id=data.get("captain_player_id")
                ),
            )
        except MatchRevisionConflictError as exc:
            return Response(
                {
                    "code": "revision_conflict",
                    "detail": "The match changed. Refresh it and try again.",
                    "expected_revision": exc.expected_revision,
                    "live_revision": exc.live_revision,
                },
                status=409,
            )
        except MatchData.DoesNotExist:
            return Response(
                {"code": "not_found", "detail": "Match tracker data was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except MatchFormError as exc:
            return Response(
                {
                    "code": exc.code,
                    "detail": _FORM_ERROR_DETAILS.get(
                        exc.code,
                        "The match form cannot be updated in its current state.",
                    ),
                },
                status=_FORM_ERROR_STATUSES.get(exc.code, status.HTTP_409_CONFLICT),
            )
        return Response(MatchFormJobSerializer(job).data, status=202)
