from django.urls import path

from . import views

urlpatterns = [
    path("detail/<uuid:team_id>", views.team_detail, name="team_detail"),
    path("register/<uuid:team_id>/", views.register_to_team, name="team_registration"),
]
