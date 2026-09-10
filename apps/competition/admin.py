"""Read-only visibility into imported KNKV data and discovery progress."""

from datetime import date

from django.contrib import admin
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.template.response import TemplateResponse
from django.urls import URLPattern, path
from django.utils import timezone

from apps.competition.models import (
    Club,
    HistoricalDiscovery,
    HistoricalResource,
    Match,
    Pool,
    ResultRevision,
    SeasonBinding,
    SyncLease,
    SyncResource,
    SyncRun,
    Team,
    TeamGroup,
    TrafficState,
)
from apps.competition.queries.monitoring import monitoring_dashboard
from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.schedule.models import Season
from apps.schedule.queries.seasons import current_season


class CatalogueAdmin(KorfbalModelAdmin):
    """Keep provider data inspectable without bypassing import/linking invariants."""

    actions = None
    list_per_page = 50
    show_full_result_count = False

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Keep provider identity creation in the importer."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        """Use reconciliation to change identity links."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        """Retain source history and discovery checkpoints."""
        return False


@admin.register(Club)
class ClubAdmin(CatalogueAdmin):
    """Browse imported clubs and their existing app identities."""

    list_display = ("name", "city", "external_id", "local_club")
    search_fields = ("name", "city", "external_id", "local_club__name")
    list_select_related = ("local_club",)


@admin.register(TeamGroup)
class TeamGroupAdmin(CatalogueAdmin):
    """Inspect shared indoor/outdoor teams and their local links."""

    list_display = ("name", "club", "season", "local_team", "local_team_data")
    search_fields = ("name", "club__name")
    list_filter = ("season",)
    list_select_related = (
        "club",
        "season",
        "local_team__club",
        "local_team_data__team",
    )


@admin.register(Team)
class TeamAdmin(CatalogueAdmin):
    """Browse distinct provider variants of shared teams."""

    list_display = ("name", "club", "sport", "season", "external_id", "group")
    search_fields = ("name", "club__name", "external_id")
    list_filter = ("season", "sport")
    list_select_related = ("club", "season", "group")


@admin.register(Pool)
class PoolAdmin(CatalogueAdmin):
    """Inspect poule coverage, freshness and local identities."""

    list_display = (
        "name",
        "class_name",
        "sport",
        "season",
        "results_filtered",
        "standings_synced_at",
        "local_pool",
    )
    search_fields = ("name", "class_name", "external_id")
    list_filter = ("season", "sport", "results_filtered")
    list_select_related = ("season", "local_pool__season")


@admin.register(Match)
class MatchAdmin(CatalogueAdmin):
    """Inspect fixtures, official results and links to recorded matches."""

    list_display = (
        "external_id",
        "starts_at",
        "home_team",
        "away_team",
        "status",
        "home_score",
        "away_score",
        "local_match",
    )
    search_fields = ("external_id", "home_team__name", "away_team__name")
    list_filter = ("season", "status", "home_team__sport")
    list_select_related = (
        "home_team",
        "away_team",
        "local_match__home_team",
        "local_match__away_team",
    )


@admin.register(SyncResource)
class SyncResourceAdmin(CatalogueAdmin):
    """Show collection discovery, refresh deadlines and failures."""

    list_display = (
        "kind",
        "source_id",
        "season",
        "fetched_at",
        "next_sync_at",
        "failures",
        "last_error",
    )
    search_fields = ("source_id", "last_error")
    list_filter = ("kind", "season")
    list_select_related = ("season",)
    change_list_template = "admin/competition/syncresource/change_list.html"

    def get_urls(self) -> list[URLPattern]:
        """Keep the monitor inside Django's authenticated admin boundary."""
        return [
            path(
                "monitor/",
                self.admin_site.admin_view(self.monitor),
                name="competition_monitor",
            ),
            *super().get_urls(),
        ]

    def monitor(self, request: HttpRequest) -> HttpResponse:
        """Render date-scoped visibility without provider I/O.

        Raises:
            PermissionDenied: The operator cannot view every exposed model.

        """
        if not isinstance(request.user, PermissionsMixin):
            raise PermissionDenied
        if not request.user.has_perms([
            "competition.view_syncresource",
            "competition.view_syncrun",
            "competition.view_match",
            "competition.view_trafficstate",
            "competition.view_synclease",
        ]):
            raise PermissionDenied
        try:
            day = (
                date.fromisoformat(request.GET["date"])
                if request.GET.get("date")
                else timezone.localdate()
            )
            if day == date.max:
                return HttpResponseBadRequest("Choose a date before 9999-12-31.")
            season = (
                Season.objects.get(pk=request.GET["season"])
                if request.GET.get("season")
                else current_season()
            )
        except (ValueError, ValidationError, Season.DoesNotExist):
            return HttpResponseBadRequest("Choose a valid season and date.")
        if season is None:
            season = Season.objects.order_by("-start_date").first()
        context = {
            **self.admin_site.each_context(request),
            "title": "Competition monitoring",
            "opts": self.model._meta,
            "seasons": Season.objects.order_by("-start_date"),
            "monitor": monitoring_dashboard(season, day) if season else None,
        }
        return TemplateResponse(request, "admin/competition/monitor.html", context)


@admin.register(SyncRun)
class SyncRunAdmin(CatalogueAdmin):
    """Inspect sanitized scheduled run outcomes and saved backlog snapshots."""

    list_display = ("started_at", "finished_at", "season", "status")
    list_filter = ("season", "status")
    list_select_related = ("season",)
    search_fields = ("season__name", "status")
    ordering = ("-started_at",)
    date_hierarchy = "started_at"


@admin.register(ResultRevision)
class ResultRevisionAdmin(CatalogueAdmin):
    """Keep official score corrections visible without editing history."""

    list_display = ("match", "observed_at", "status", "home_score", "away_score")
    search_fields = ("match__external_id",)
    list_select_related = ("match",)


@admin.register(TrafficState)
class TrafficStateAdmin(CatalogueAdmin):
    """Show the shared provider request counters."""

    list_display = ("key", "hour_requests", "day_requests", "next_request_at")
    search_fields = ("key",)


@admin.register(SyncLease)
class SyncLeaseAdmin(CatalogueAdmin):
    """Show whether an importer lease or cooldown is active."""

    list_display = ("key", "owner", "expires_at")
    search_fields = ("key", "owner")


@admin.register(HistoricalResource)
class HistoricalResourceAdmin(CatalogueAdmin):
    """Inspect transport checkpoints separately from historical completeness."""

    list_display = (
        "provider",
        "kind",
        "source_id",
        "season",
        "start_date",
        "end_date",
        "state",
        "coverage",
        "reason",
        "attempts",
        "fetched_at",
    )
    list_filter = ("provider", "kind", "season", "state", "coverage")
    search_fields = ("source_id", "reason")
    list_select_related = ("season",)


@admin.register(HistoricalDiscovery)
class HistoricalDiscoveryAdmin(CatalogueAdmin):
    """Keep source attribution and discovery links available for audit."""

    list_display = ("resource", "parent", "reference")
    list_select_related = ("resource", "parent")
    search_fields = ("reference", "resource__source_id")


@admin.register(SeasonBinding)
class SeasonBindingAdmin(CatalogueAdmin):
    """Inspect source scopes and native season mappings."""

    list_display = ("scope", "sport", "season")
    search_fields = ("scope__name", "season__name", "sport")
    list_select_related = ("scope", "season")
