from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    
    path('teams/', views.teams, name='teams'),
    path('team/<uuid:team_id>', views.team_detail, name='team_detail'),
    
    path('profile/<uuid:player_id>/', views.profile_detail, name='profile_detail'),
    path('upload_profile_picture/', views.upload_profile_picture, name='upload_profile_picture'),
    
    path('match/<uuid:match_id>/', views.match_detail, name='match_detail'),
]
