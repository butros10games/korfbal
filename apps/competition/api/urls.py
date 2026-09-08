"""Competition catalogue routes."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from .ratings import RatingsView
from .views import (
    AllocationViewSet,
    ClubViewSet,
    MatchViewSet,
    PoolViewSet,
    ResourceViewSet,
    SeasonViewSet,
    TeamGroupViewSet,
    TeamViewSet,
)


router = DefaultRouter()
router.register("allocations", AllocationViewSet, basename="competition-allocation")
router.register("clubs", ClubViewSet, basename="competition-club")
router.register("team-groups", TeamGroupViewSet, basename="competition-team-group")
router.register("teams", TeamViewSet, basename="competition-team")
router.register("pools", PoolViewSet, basename="competition-pool")
router.register("matches", MatchViewSet, basename="competition-match")
router.register("sync-resources", ResourceViewSet, basename="competition-resource")
router.register("seasons", SeasonViewSet, basename="competition-season")
urlpatterns = [
    path("ratings/", RatingsView.as_view(), name="competition-ratings"),
    *router.urls,
]
