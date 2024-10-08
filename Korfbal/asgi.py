import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Korfbal.settings')
django_asgi_app = get_asgi_application()

from apps.game_tracker.routing import websocket_urlpatterns as game_tracker_routing
from apps.club.routing import websocket_urlpatterns as club_routing
from apps.player.routing import websocket_urlpatterns as player_routing
from apps.team.routing import websocket_urlpatterns as team_routing

all_websocket_urlpatterns = game_tracker_routing + club_routing + player_routing + team_routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
            all_websocket_urlpatterns
        )
    ),
})
