from django.urls import path
from .consumers import ProfileDataConsumer


websocket_urlpatterns = [
    path('ws/profile/<uuid:id>/', ProfileDataConsumer.as_asgi()),
]
