"""Searchable relation filters without materializing the related catalogue."""

from typing import Any

from django import forms
from django.contrib import admin
from django.contrib.admin.options import IncorrectLookupParameters
from django.contrib.admin.utils import get_fields_from_path
from django.contrib.admin.widgets import AutocompleteSelect
from django.core.exceptions import ValidationError
from django.db import models
from django.http import HttpRequest


class RelatedAutocompleteFilter(admin.SimpleListFilter):
    """Apply one exact relation selection while preserving all other list controls."""

    template = "admin/korfbal/related_filter.html"
    field_path = ""
    parameter_name: str = ""

    def __init__(
        self,
        request: HttpRequest,
        params: dict[str, Any],
        model: type[models.Model],
        model_admin: admin.ModelAdmin,
    ) -> None:
        """Build a selected-only widget for this relation.

        Raises:
            IncorrectLookupParameters: If the selected primary key is malformed.

        """
        field = get_fields_from_path(model, self.field_path)[-1]
        assert isinstance(field, (models.ForeignKey, models.ManyToManyField))
        target_field = field.remote_field.model._meta.pk
        self.parameter_name = f"{self.field_path}__{target_field.name}__exact"
        super().__init__(request, params, model, model_admin)
        self.hidden_parameters = [
            (key, value)
            for key, values in request.GET.lists()
            if key not in {self.parameter_name, "p", "e"}
            for value in values
        ]
        try:
            selected = target_field.to_python(self.value()) if self.value() else None
        except (ValueError, ValidationError) as error:
            raise IncorrectLookupParameters from error
        target_admin = model_admin.admin_site.get_model_admin(field.remote_field.model)
        widget = AutocompleteSelect(field, model_admin.admin_site)
        form_field = forms.ModelChoiceField(
            queryset=target_admin.get_queryset(request),
            widget=widget,
            required=False,
        )
        self.widget = form_field.widget.render(
            self.parameter_name,
            selected,
            attrs={
                "id": f"filter-{self.field_path}",
                "aria-label": self.title,
                "data-placeholder": f"Search {str(self.title).lower()}",
            },
        )
        self.media = form_field.widget.media

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin
    ) -> tuple[tuple[str, str], ...]:
        """Keep the search control visible even when nothing is selected."""
        return (("", "All"),)

    def queryset(
        self, request: HttpRequest, queryset: models.QuerySet
    ) -> models.QuerySet:
        """Filter by a validated primary key; never enumerate all relation choices."""
        return (
            queryset.filter(**{self.parameter_name: self.value()})
            if self.value()
            else queryset
        )


def relation_filter(field_path: str, title: str) -> type[RelatedAutocompleteFilter]:
    """Declare a named relation filter beside its model admin configuration."""
    return type(
        f"{field_path.replace('__', '_')}Filter",
        (RelatedAutocompleteFilter,),
        {"field_path": field_path, "title": title, "parameter_name": field_path},
    )
