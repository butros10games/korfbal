"""Shared admin query and form behavior for Korfbal's registered models."""

from typing import Any
from uuid import UUID

from django.contrib import admin
from django.db import models
from django.http import HttpRequest, HttpResponse


class KorfbalModelAdmin[ModelT: models.Model](admin.ModelAdmin):
    """Bound list pages and load explicitly declared label relations on every read."""

    list_per_page = 50
    list_max_show_all = 0
    show_full_result_count = False
    show_facets = admin.ShowFacets.NEVER
    save_on_top = True
    empty_value_display = "—"
    search_help_text = "Search by name or paste a complete record ID."

    def get_readonly_fields(
        self, request: HttpRequest, obj: ModelT | None = None
    ) -> list[str] | tuple[str, ...]:
        """Keep full UUIDs on detail pages without leading every list with them."""
        fields = tuple(super().get_readonly_fields(request, obj))
        pk = self.model._meta.pk
        if (
            isinstance(pk, models.UUIDField)
            and not pk.editable
            and pk.name not in fields
        ):
            return (*fields, pk.name)
        return fields

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[ModelT]:
        """Reuse label joins for lists, object forms and autocomplete responses."""
        queryset = super().get_queryset(request)
        if self.list_select_related and isinstance(
            self.list_select_related, (list, tuple)
        ):
            return queryset.select_related(*self.list_select_related)
        return queryset

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        """Merge autocomplete filter assets once with Django's existing form media."""
        response = super().changelist_view(request, extra_context)
        context = getattr(response, "context_data", None)
        if context and "cl" in context:
            for spec in context["cl"].filter_specs:
                if hasattr(spec, "media"):
                    context["media"] += spec.media
        return response

    def get_autocomplete_fields(
        self, request: HttpRequest
    ) -> list[str] | tuple[str, ...]:
        """Avoid full-table dropdowns when a permission-checked search is available."""
        fields = list(super().get_autocomplete_fields(request))
        for field in self.model._meta.get_fields():
            if (
                field.auto_created
                or not field.editable
                or not isinstance(field, (models.ForeignKey, models.ManyToManyField))
                or field.name in self.raw_id_fields
                or field.name in fields
            ):
                continue
            target = self.admin_site._registry.get(field.remote_field.model)
            if target is not None and target.search_fields:
                fields.append(field.name)
        return tuple(fields)

    def get_search_fields(self, request: HttpRequest) -> list[str] | tuple[str, ...]:
        """Text searches must not cast every UUID column to a substring."""
        return tuple(
            field
            for field in super().get_search_fields(request)
            if not field.lstrip("^=@").endswith("id_uuid")
        )

    def get_search_results(
        self,
        request: HttpRequest,
        queryset: models.QuerySet[ModelT],
        search_term: str,
    ) -> tuple[models.QuerySet[ModelT], bool]:
        """Use typed exact lookups for full UUIDs and normal name search otherwise."""
        paths = [
            field.lstrip("^=@")
            for field in self.search_fields
            if field.lstrip("^=@").endswith("id_uuid")
        ]
        if isinstance(self.model._meta.pk, models.UUIDField):
            paths.append(self.model._meta.pk.name)
        try:
            identifier = UUID(search_term.strip())
        except ValueError:
            return super().get_search_results(request, queryset, search_term)
        if not paths:
            return super().get_search_results(request, queryset, search_term)
        predicate = models.Q()
        for path in set(paths):
            predicate |= models.Q(**{path: identifier})
        return queryset.filter(predicate), False
