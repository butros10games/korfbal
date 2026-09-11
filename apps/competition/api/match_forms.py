"""Private status and durable commands for a connected account's match forms."""

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers
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
            return Response({"code": "not_connected"}, status=403)
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
                {"code": "revision_conflict", "live_revision": exc.live_revision},
                status=409,
            )
        except MatchData.DoesNotExist:
            return Response({"code": "not_connected"}, status=404)
        except MatchFormError as exc:
            return Response({"code": exc.code}, status=409)
        return Response(MatchFormJobSerializer(job).data, status=202)
