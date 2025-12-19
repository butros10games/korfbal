"""URL routes for hub API.

This module also provides backwards-compatible URL names that older code/tests
expect to exist (e.g. `index`, `api_catalog_data`). These names now map to
API endpoints, not server-rendered templates.

"""

from __future__ import annotations

from django.urls import path

from .views import CatalogDataView, HubIndexView, UpdateFeedView


urlpatterns = [
    path("index/", HubIndexView.as_view(), name="index"),
    path("catalog_data/", CatalogDataView.as_view(), name="api_catalog_data"),
    path("updates/", UpdateFeedView.as_view(), name="hub-updates"),
]
