from django.urls import path
from . import views

urlpatterns = [
    path('', views.teams, name='teams'),
    path('detail/<uuid:team_id>', views.team_detail, name='team_detail'),
    path('indexdata/', views.teams_index_data, name='teams_index_data'),
    path('register/<uuid:team_id>/', views.register_to_team, name='team_registration'),
]