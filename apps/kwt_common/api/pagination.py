"""Shared DRF pagination helpers.

We do not currently have global DRF pagination defaults configured.
To avoid accidental "return everything" list endpoints (which get slower as the
DB grows), we opt in per-viewset.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from django.db.models import Model, QuerySet
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.views import APIView


_Model = TypeVar("_Model", bound=Model)
_Row = TypeVar("_Row")


def ensure_totally_ordered[QueryModel: Model, QueryRow](
    queryset: QuerySet[QueryModel, QueryRow],
) -> QuerySet[QueryModel, QueryRow]:
    """Add the primary key as a stable tie-breaker when ordering is ambiguous."""
    if queryset.totally_ordered:
        return queryset

    ordering = tuple(queryset.query.order_by)
    if not ordering:
        ordering = tuple(queryset.model._meta.ordering or ())

    primary_key = queryset.model._meta.pk.name
    return queryset.order_by(*ordering, primary_key)


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

    def paginate_queryset(
        self,
        queryset: QuerySet[_Model, _Row] | Sequence[_Row],
        request: Request,
        view: APIView | None = None,
    ) -> list[_Row] | None:
        """Paginate with deterministic boundaries for non-unique ordering."""
        if isinstance(queryset, QuerySet):
            queryset = ensure_totally_ordered(queryset)
        return super().paginate_queryset(queryset, request, view)
