"""Push-subscription API views for the player app."""

from __future__ import annotations

from typing import Any, cast

from rest_framework import permissions, status
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.api.serializers import (
    PlayerPushSubscriptionCreateSerializer,
    PlayerPushSubscriptionDeactivateSerializer,
    PlayerPushSubscriptionSerializer,
)
from apps.player.composition import (
    send_test_web_pushes,
    webpush_library_available,
)
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.push_notifications import (
    NoActivePushSubscriptionsError,
    PushTestConfigurationError,
    send_test_push_notification,
)
from apps.player.services.push_subscriptions import (
    PushSubscriptionNotFoundError,
    deactivate_push_subscription,
    register_push_subscription,
)

from .common import TEST_PUSH_ERROR_LIMIT


class CurrentPlayerPushSubscriptionsAPIView(KorfbalAPIView):
    """Register/list/deactivate push subscriptions for the current user."""

    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = (JSONParser,)

    def get(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """List active push subscriptions for the current user."""
        subs = PlayerPushSubscription.objects.filter(
            user=request.user,
            is_active=True,
        ).order_by("-updated_at")
        return Response(PlayerPushSubscriptionSerializer(subs, many=True).data)

    def post(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Register or upsert a push subscription for the current user."""
        serializer = PlayerPushSubscriptionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        subscription = cast(
            dict[str, Any],
            serializer.validated_data["subscription"],
        )
        user_agent = str(serializer.validated_data.get("user_agent") or "").strip()
        platform = str(serializer.validated_data.get("platform") or "web").strip()
        registration = register_push_subscription(
            user_id=cast(int, request.user.pk),
            subscription=subscription,
            platform=platform,
            user_agent=user_agent,
        )
        payload = PlayerPushSubscriptionSerializer(registration.subscription).data
        return Response(
            {"created": registration.created, "subscription": payload},
            status=(
                status.HTTP_201_CREATED if registration.created else status.HTTP_200_OK
            ),
        )

    def delete(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Deactivate a stored push subscription for the current user."""
        serializer = PlayerPushSubscriptionDeactivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        endpoint = serializer.validated_data.get("endpoint")
        sub_id = serializer.validated_data.get("id_uuid")

        try:
            deactivate_push_subscription(
                user_id=cast(int, request.user.pk),
                endpoint=str(endpoint) if endpoint else None,
                subscription_id=sub_id,
            )
        except PushSubscriptionNotFoundError:
            return Response(
                {"detail": "Subscription not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentPlayerTestPushNotificationAPIView(KorfbalAPIView):
    """Send a test push notification to the current user's active subscriptions."""

    permission_classes = (permissions.IsAuthenticated,)

    @staticmethod
    def _is_staff_user(user: Any) -> bool:
        return bool(
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        )

    def post(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Send a test push notification to the current user's subscriptions."""
        user = request.user
        if not self._is_staff_user(user):
            return Response(
                {"detail": "Staff only"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            result = send_test_push_notification(
                user_id=cast(int, user.pk),
                webpush_available=webpush_library_available,
                send_pushes=send_test_web_pushes,
            )
        except PushTestConfigurationError as exc:
            return Response(
                {
                    "detail": exc.detail,
                    "missing": exc.missing,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except NoActivePushSubscriptionsError:
            return Response(
                {"detail": "No active push subscriptions"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_payload: dict[str, Any] = {
            "total": result.total,
            "sent": result.sent,
            "failed": result.failed,
        }
        if result.errors:
            response_payload["errors"] = result.errors[:TEST_PUSH_ERROR_LIMIT]
            if len(result.errors) > TEST_PUSH_ERROR_LIMIT:
                response_payload["errors_truncated"] = True

        return Response(response_payload, status=status.HTTP_200_OK)
