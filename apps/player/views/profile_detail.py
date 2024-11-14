from django.shortcuts import render, get_object_or_404

from apps.player.models import Player


def profile_detail(request, player_id=None):
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
        "display_back": display_back
    }

    return render(request, "profile/index.html", context)
