"""Serve the service worker script."""

import os
import pathlib

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse


def service_worker(_request: HttpRequest) -> HttpResponse:
    """Serve the service worker script from the root.

    Args:
        _request: The HTTP request.

    Returns:
        HttpResponse: The service worker JavaScript file.

    Raises:
        Http404: If the service worker file is not found.

    """
    # Try to find the file in STATIC_ROOT (production)
    path = (
        os.path.join(settings.STATIC_ROOT, "webpack_bundles/sw.bundle.js")
        if settings.STATIC_ROOT
        else None
    )

    if not path or not os.path.exists(path):
        # Try finders for development
        from django.contrib.staticfiles import finders  # noqa: PLC0415

        path = finders.find("webpack_bundles/sw.bundle.js")

    if not path or not os.path.exists(path):
        raise Http404("Service Worker not found")

    content = pathlib.Path(path).read_bytes()

    return HttpResponse(content, content_type="application/javascript")
