"""Module contains the view for the player profile detail page."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from apps.player.models import Player


def profile_detail(request: HttpRequest, player_id: str | None = None) -> HttpResponse:
    """View for the player profile detail page.

    Args:
        request (HttpRequest): The request object.
        player_id (str): The UUID of the player.

    Returns:
        HttpResponse: The response object.

    """
    player: Player | None = None
    user = request.user

    if player_id:
        player = get_object_or_404(Player, id_uuid=player_id)
    elif user.is_authenticated:
        # Use filter().first() so we get None if no Player exists for the user
        player = Player.objects.filter(user=user).first()

    # Check if the user is viewing their own profile
    is_own_profile: bool = False
    # Ensure player is not None before accessing player.user
    if player is not None and user.is_authenticated and player.user == user:
        is_own_profile = True

    display_back: bool = False
    if is_own_profile:
        display_back = True

    context = {
        "player": player,
        "profile_picture": player.get_profile_picture() if player else None,
        "is_own_profile": is_own_profile,
        "display_back": display_back,
    }

    return render(request, "profile/index.html", context)
