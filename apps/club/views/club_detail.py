from apps.club.models import Club
from apps.player.models import Player
from django.shortcuts import get_object_or_404, render


def club_detail(request, club_id):
    club = get_object_or_404(Club, id_uuid=club_id)

    user_request = request.user
    admin = False
    following = False
    if user_request.is_authenticated:
        player = Player.objects.get(user=user_request)
        admin = club.admin.filter(id_uuid=player.id_uuid).exists()

        following = player.club_follow.filter(id_uuid=club_id).exists()

    context = {"club": club, "admin": admin, "following": following}

    return render(request, "club/detail.html", context)
