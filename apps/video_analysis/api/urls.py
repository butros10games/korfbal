"""Native video analysis API, including authenticated artifacts and media."""

from django.urls import path

from .views import endpoint


urlpatterns = [path("<path:action>", endpoint, name="video-analysis")]
