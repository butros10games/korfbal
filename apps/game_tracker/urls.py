from django.urls import path
from . import views

urlpatterns = [
    path('<uuid:match_id>/', views.match_detail, name='match_detail'),
    path('selector/<uuid:match_id>/', views.match_team_selector, name='match_team_selector'),
    path('tracker/<uuid:match_id>/<uuid:team_id>/', views.match_tracker, name='match_tracker'),
    
    path('player_overview/<uuid:match_id>/<uuid:team_id>/', views.player_overview, name='player_overview'),
    
    path('api/player_search/', views.player_search, name='player_search'),
    path('api/player_selection/<uuid:match_id>/<uuid:team_id>/', views.player_selection, name='player_selection'),
    path('api/player_designation/', views.player_designation, name='player_designation'),
]
