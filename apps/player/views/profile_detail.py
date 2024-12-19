"""This module contains the view for the player profile detail page."""

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from apps.player.models import Player


def profile_detail(request, player_id=None) -> HttpResponse:
    """View for the player profile detail page.

    Args:
        request (HttpRequest): The request object.
        player_id (str): The UUID of the player.

    Returns:
        HttpResponse: The response object.
    """
    player = None
    user = request.user

    if player_id:
        player = get_object_or_404(Player, id_uuid=player_id)
    elif user.is_authenticated:
        player = Player.objects.get(user=user)

    # Check if the user is viewing their own profile
    is_own_profile = False
    if user.is_authenticated and user == player.user:
        is_own_profile = True

    display_back = False
    if is_own_profile:
        display_back = True

    context = {
        "player": player,
        "profile_picture": player.get_profile_picture() if player else None,
        "is_own_profile": is_own_profile,
        "display_back": display_back,
    }

    return render(request, "profile/index.html", context)
