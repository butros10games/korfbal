"""Push-subscription API views for the player app."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import transaction
from rest_framework import permissions, status
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.player.api.serializers import (
    PlayerPushSubscriptionCreateSerializer,
    PlayerPushSubscriptionDeactivateSerializer,
    PlayerPushSubscriptionSerializer,
)
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.push_notifications import (
    build_target_url,
    missing_webpush_settings,
    send_test_payload,
)
from apps.player.services.web_push import WebPushPayload, webpush_library_available

from .common import TEST_PUSH_ERROR_LIMIT


class CurrentPlayerPushSubscriptionsAPIView(APIView):
    """Register/list/deactivate push subscriptions for the current user."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]
    parser_classes: ClassVar[list[type[Any]]] = [JSONParser]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """List active push subscriptions for the current user."""
        subs = PlayerPushSubscription.objects.filter(
            user=request.user,
            is_active=True,
        ).order_by("-updated_at")
        return Response(PlayerPushSubscriptionSerializer(subs, many=True).data)

    @transaction.atomic
    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Register or upsert a push subscription for the current user."""
        serializer = PlayerPushSubscriptionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        subscription = serializer.validated_data["subscription"]
        if not isinstance(subscription, dict):
            return Response(
                {"detail": "Invalid subscription payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        endpoint = str(subscription.get("endpoint") or "").strip()
        user_agent = str(serializer.validated_data.get("user_agent") or "").strip()
        platform = str(serializer.validated_data.get("platform") or "web").strip()
        if not endpoint:
            return Response(
                {"detail": "subscription.endpoint is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        obj = PlayerPushSubscription.objects.filter(endpoint=endpoint).first()
        created = False
        if obj is None:
            obj = PlayerPushSubscription.objects.create(
                user=request.user,
                endpoint=endpoint,
                subscription=subscription,
                platform=platform,
                is_active=True,
                user_agent=user_agent,
            )
            created = True
        else:
            obj.user = request.user
            obj.subscription = subscription
            obj.platform = platform
            obj.is_active = True
            obj.user_agent = user_agent
            obj.save(
                update_fields=[
                    "user",
                    "subscription",
                    "platform",
                    "is_active",
                    "user_agent",
                    "updated_at",
                ]
            )

        payload = PlayerPushSubscriptionSerializer(obj).data
        return Response(
            {"created": created, "subscription": payload},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @transaction.atomic
    def delete(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Deactivate a stored push subscription for the current user."""
        serializer = PlayerPushSubscriptionDeactivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        endpoint = serializer.validated_data.get("endpoint")
        sub_id = serializer.validated_data.get("id_uuid")

        queryset = PlayerPushSubscription.objects.filter(user=request.user)
        if endpoint:
            queryset = queryset.filter(endpoint=endpoint)
        if sub_id:
            queryset = queryset.filter(id_uuid=sub_id)

        obj = queryset.first()
        if obj is None:
            return Response(
                {"detail": "Subscription not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.is_active:
            obj.is_active = False
            obj.save(update_fields=["is_active", "updated_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentPlayerTestPushNotificationAPIView(APIView):
    """Send a test push notification to the current user's active subscriptions."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    @staticmethod
    def _is_staff_user(user: Any) -> bool:  # noqa: ANN401
        return bool(
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        )

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Send a test push notification to the current user's subscriptions."""
        user = request.user
        if not self._is_staff_user(user):
            return Response(
                {"detail": "Staff only"},
                status=status.HTTP_403_FORBIDDEN,
            )

        missing = missing_webpush_settings()
        if missing:
            return Response(
                {
                    "detail": "Web push not configured",
                    "missing": missing,
                },
                status=status.HTTP_409_CONFLICT,
            )

        if not webpush_library_available():
            return Response(
                {
                    "detail": "Web push runtime is missing pywebpush",
                    "missing": ["pywebpush"],
                },
                status=status.HTTP_409_CONFLICT,
            )

        subs_qs = PlayerPushSubscription.objects.filter(
            user=user,
            is_active=True,
        ).order_by("-updated_at")
        if not subs_qs.exists():
            return Response(
                {"detail": "No active push subscriptions"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sent, failed, errors = send_test_payload(
            subs=list(subs_qs),
            payload=WebPushPayload(
                title="Test pushmelding",
                body="Als je dit ziet werkt push via de PWA.",
                url=build_target_url(),
                tag="debug-test",
            ),
        )

        response_payload: dict[str, Any] = {
            "total": subs_qs.count(),
            "sent": sent,
            "failed": failed,
        }
        if errors:
            response_payload["errors"] = errors[:TEST_PUSH_ERROR_LIMIT]
            if len(errors) > TEST_PUSH_ERROR_LIMIT:
                response_payload["errors_truncated"] = True

        return Response(response_payload, status=status.HTTP_200_OK)
