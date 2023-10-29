from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    
    path('club/<uuid:club_id>/', views.club_detail, name='club_detail'),
    
    path('teams/', views.teams, name='teams'),
    path('team/<uuid:team_id>', views.team_detail, name='team_detail'),
    path('search/', views.search, name='search'),
    path('teams/indexdata/', views.teams_index_data, name='teams_index_data'),
    
    path('profile/<uuid:player_id>/', views.profile_detail, name='profile_detail'),
    path('upload_profile_picture/', views.upload_profile_picture, name='upload_profile_picture'),
    
    path('match/<uuid:match_id>/', views.match_detail, name='match_detail'),
    path('match/selector/<uuid:match_id>/', views.match_team_selector, name='match_team_selector'),
    path('match/tracker/<uuid:match_id>/<uuid:team_id>/', views.match_tracker, name='match_tracker'),
]
