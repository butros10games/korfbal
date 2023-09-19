from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path('ws/teams/<uuid:id>/', consumers.team_data.as_asgi()),
]