from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path('ws/club/<uuid:id>/', consumers.club_data.as_asgi())
]