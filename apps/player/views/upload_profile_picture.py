"""View to upload a profile picture for a player."""

from django.http import HttpRequest, JsonResponse

from apps.player.models import Player


def upload_profile_picture(request: HttpRequest) -> JsonResponse:
    """Upload a profile picture for a player."""
    if request.method == "POST" and request.FILES["profile_picture"]:
        profile_picture = request.FILES["profile_picture"]

        # Assuming you have a Player model with a profile_picture field
        player: Player = Player.objects.get(user=request.user)
        player.profile_picture.save(profile_picture.name, profile_picture)

        # Return the URL of the uploaded image
        return JsonResponse({"url": player.get_profile_picture()})

    return JsonResponse({"error": "Invalid request"}, status=400)
