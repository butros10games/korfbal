"""Context processor to add profile_url and profile_img_url to the context."""

from apps.player.models import Player


def standart_imports(request):
    """
    Add profile_url and profile_img_url to the context.

    Args:
        request: The request object.

    Returns:
        A dictionary containing the profile_url and profile_img_url.
    """
    profile_url = None
    profile_img_url = None
    user_request = request.user

    if user_request.is_authenticated:
        try:
            player = Player.objects.get(user=user_request)
            profile_url = player.get_absolute_url
            profile_img_url = player.get_profile_picture()
        except Player.DoesNotExist:
            # Player object does not exist for this user, return None for profile_url
            # and profile_img_url
            pass

    return {"profile_url": profile_url, "profile_img_url": profile_img_url}
