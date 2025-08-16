"""View to upload a profile picture for a player."""

from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, JsonResponse

from apps.player.models import Player


def upload_profile_picture(request: HttpRequest) -> JsonResponse:
    """Upload a profile picture for a player.

    Args:
        request (HttpRequest): The HTTP request containing the profile picture.

    Returns:
        JsonResponse: A JSON response containing URL of the uploaded profile picture.

    """
    if request.method == "POST":
        files = request.FILES.getlist("profile_picture")
        if not files:
            return JsonResponse({"error": "No profile_picture uploaded"}, status=400)

        profile_picture = files[0]

        if not (
            isinstance(profile_picture, UploadedFile)
            or hasattr(profile_picture, "name")
        ):
            return JsonResponse({"error": "Invalid uploaded file"}, status=400)

        try:
            player: Player = Player.objects.get(user=request.user)
        except Player.DoesNotExist:
            return JsonResponse({"error": "Player not found"}, status=404)

        filename = getattr(profile_picture, "name", "profile_picture")
        player.profile_picture.save(filename, profile_picture)

        return JsonResponse({"url": player.get_profile_picture()})

    return JsonResponse({"error": "Invalid request"}, status=400)
