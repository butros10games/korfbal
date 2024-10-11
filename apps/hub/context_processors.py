from apps.player.models import Player


def standart_imports(request):
    profile_url = None
    profile_img_url = None
    user_request = request.user
    
    if user_request.is_authenticated:
        try:
            player = Player.objects.get(user=user_request)
            profile_url = player.get_absolute_url
            profile_img_url = player.profile_picture.url if player.profile_picture else None
        except Player.DoesNotExist:
            # Player object does not exist for this user, return None for profile_url and profile_img_url
            pass
    
    return {'profile_url': profile_url, 'profile_img_url': profile_img_url}