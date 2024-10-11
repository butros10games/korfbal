from django.urls import path
from . import views

urlpatterns = [
    path('<uuid:match_id>/', views.match_detail, name='match_detail'),
    path('selector/<uuid:match_id>/', views.match_team_selector, name='match_team_selector'),
    path('tracker/<uuid:match_id>/<uuid:team_id>/', views.match_tracker, name='match_tracker'),
]
