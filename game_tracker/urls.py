from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('teams/', views.teams, name='teams'),
    path('team/<uuid:team_id>', views.team_detail, name='team_detail'),
    path('profile/', views.profile, name='profile'),
]
