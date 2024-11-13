from django.urls import path
from .consumers import TeamDataConsumer


websocket_urlpatterns = [
    path('ws/teams/<uuid:id>/', TeamDataConsumer.as_asgi()),
]
