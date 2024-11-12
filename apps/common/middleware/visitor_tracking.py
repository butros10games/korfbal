from django.utils import timezone

from apps.hub.models import PageConnectRegistration
from apps.player.models import Player

class VisitorTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Process the request
        if request.user.is_authenticated:
            try:
                player = Player.objects.get(user=request.user)
                page, created = PageConnectRegistration.objects.get_or_create(
                    player=player, page=request.path)
                
                if not created:
                    # Update the registration date if it's not a new record
                    page.registration_date = timezone.now()
                    page.save()

                # Reset the back_counter only if this is not a back navigation
                if not request.session.pop('is_back_navigation', False):
                    request.session['back_counter'] = 1

            except Player.DoesNotExist:
                # Handle the case where the player does not exist
                pass
        
        # Pass the request to the next middleware or view
        response = self.get_response(request)
        
        # Return the response
        return response