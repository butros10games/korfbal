"""Shared DRF pagination helpers.

We do not currently have global DRF pagination defaults configured.
To avoid accidental "return everything" list endpoints (which get slower as the
DB grows), we opt in per-viewset.
"""

from __future__ import annotations

from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    """Default pagination for list endpoints.

    Notes:
        - `page_size` is intentionally conservative; the React app can request a
          larger size via `page_size` when needed.
        - `max_page_size` protects the API from large accidental responses.

    """

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
