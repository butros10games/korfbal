from django.urls import path
from .consumers import ClubDataConsumer

websocket_urlpatterns = [
    path('ws/club/<uuid:id>/', ClubDataConsumer.as_asgi())
]