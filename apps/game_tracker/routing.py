from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path('ws/teams/<uuid:id>/', consumers.team_data.as_asgi()),
    path('ws/profile/<uuid:id>/', consumers.profile_data.as_asgi()),
    path('ws/club/<uuid:id>/', consumers.club_data.as_asgi()),
    path('ws/match/<uuid:id>/', consumers.match_data.as_asgi()),
    path('ws/match/tracker/<uuid:id>/<uuid:team_id>/', consumers.match_tracker.as_asgi()),
]