"""Korfbal admin branding and permission-aware shortcuts."""

from typing import Any

from django.contrib.admin import AdminSite
from django.contrib.auth.models import PermissionsMixin
from django.http import HttpRequest
from django.urls import reverse


class KorfbalAdminSite(AdminSite):
    """Keep native admin permissions while organizing the operations workspace."""

    site_header = "Korfbal administration"
    site_title = "Korfbal admin"
    index_title = "Operations overview"
    index_template = "admin/korfbal/index.html"

    def each_context(self, request: HttpRequest) -> dict[str, Any]:
        """Only advertise destinations the current operator can open."""
        context = super().each_context(request)
        shortcuts = []
        for label, model_label in [
            ("Matches", "schedule.match"),
            ("Players", "player.player"),
            ("Teams", "team.teamdata"),
            ("Tournaments", "tournament.tournament"),
        ]:
            app_label, model_name = model_label.split(".")
            entry = next(
                (
                    screen
                    for model, screen in self._registry.items()
                    if model._meta.label_lower == model_label
                ),
                None,
            )
            if entry and entry.has_view_or_change_permission(request):
                shortcuts.append({
                    "label": label,
                    "url": reverse(f"admin:{app_label}_{model_name}_changelist"),
                })
        monitoring = ("syncresource", "syncrun", "match", "trafficstate", "synclease")
        if isinstance(request.user, PermissionsMixin) and all(
            request.user.has_perm(f"competition.view_{model}") for model in monitoring
        ):
            shortcuts.insert(
                0,
                {
                    "label": "Polling monitor",
                    "url": reverse("admin:competition_monitor"),
                },
            )
        return {**context, "korfbal_shortcuts": shortcuts}
