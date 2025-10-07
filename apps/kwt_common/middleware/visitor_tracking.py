"""Middleware to track the pages visited by the player."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from apps.hub.models import PageConnectRegistration
from apps.player.models import Player


class VisitorTrackingMiddleware:
    """Middleware to track the pages visited by the player."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Initialize the middleware."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Process the request.

        Returns:
            HttpResponse: The response object after processing the request.

        """
        if request.user.is_authenticated:
            try:
                player = Player.objects.get(user=request.user)
                page, created = PageConnectRegistration.objects.get_or_create(
                    player=player,
                    page=request.path,
                )

                if not created:
                    # Update the registration date if it's not a new record
                    page.registration_date = timezone.now()
                    page.save()

                # Reset the back_counter only if this is not a back navigation
                if not request.session.pop("is_back_navigation", False):
                    request.session["back_counter"] = 1

            except Player.DoesNotExist:
                # Handle the case where the player does not exist
                pass

        return self.get_response(request)
