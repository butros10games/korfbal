"""URL routes for team API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TeamViewSet


router = DefaultRouter()
router.register(r"teams", TeamViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
