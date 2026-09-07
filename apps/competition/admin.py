"""Read-only visibility into imported KNKV data and discovery progress."""

from django.contrib import admin
from django.http import HttpRequest

from apps.competition.models import (
    Club,
    Match,
    Pool,
    ResultRevision,
    SyncLease,
    SyncResource,
    Team,
    TeamGroup,
    TrafficState,
)


class CatalogueAdmin(admin.ModelAdmin):
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


@admin.register(SyncLease)
class SyncLeaseAdmin(CatalogueAdmin):
    """Show whether an importer lease or cooldown is active."""

    list_display = ("key", "owner", "expires_at")
