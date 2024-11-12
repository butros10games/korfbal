from django.urls import path
from .consumers import MatchDataConsumer, MatchTrackerConsumer

websocket_urlpatterns = [
    path('ws/match/<uuid:id>/', MatchDataConsumer.as_asgi()),
    path('ws/match/tracker/<uuid:id>/<uuid:team_id>/', MatchTrackerConsumer.as_asgi()),
]