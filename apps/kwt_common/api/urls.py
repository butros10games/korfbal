"""kwt_common API endpoints.

These are internal/operational endpoints, intended for admins.
"""

from django.urls import path

from .views import SlowRequestsAPIView


urlpatterns = [
    path("slow-requests/", SlowRequestsAPIView.as_view(), name="slow-requests"),
]
