"""Searchable tournament planning, scoring and audit screens."""

from typing import ClassVar

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter
from apps.tournament.models import (
    Tournament,
    TournamentDisplayConfig,
    TournamentField,
    TournamentFinalGroup,
    TournamentMatch,
    TournamentMember,
    TournamentPool,
    TournamentPoolEntry,
    TournamentResultAudit,
    TournamentStage,
    TournamentStandingAdjustment,
    TournamentTeam,
)


@admin.register(Tournament)
class TournamentAdmin(KorfbalModelAdmin):
    """Find an event by name, date or operational state."""

    list_display = ("name", "starts_at", "status", "visibility", "location", "owner")
    search_fields = ("id_uuid", "name", "slug", "location", "owner__username")
    list_filter = ("status", "visibility", "starts_at")
    list_select_related = ("owner", "organizer_club")
    readonly_fields = ("created_at", "updated_at", "live_revision", "live_changed_at")
    prepopulated_fields: ClassVar[dict[str, tuple[str, ...]]] = {"slug": ("name",)}


@admin.register(TournamentField)
class TournamentFieldAdmin(KorfbalModelAdmin):
    """Browse fields without loading every event into a dropdown."""

    list_display = ("label", "tournament", "sort_order", "active")
    search_fields = ("id_uuid", "label", "tournament__name")
    list_filter = (relation_filter("tournament", "Tournament"), "active")
    list_select_related = ("tournament",)


@admin.register(TournamentTeam)
class TournamentTeamAdmin(KorfbalModelAdmin):
    """Locate entrants and their check-in or withdrawal state."""

    list_display = (
        "name",
        "tournament",
        "affiliation",
        "seed",
        "checked_in",
        "withdrawn",
    )
    search_fields = ("id_uuid", "name", "short_name", "affiliation", "tournament__name")
    list_filter = (
        relation_filter("tournament", "Tournament"),
        "checked_in",
        "withdrawn",
    )
    list_select_related = ("tournament", "linked_team__club")


@admin.register(TournamentFinalGroup)
class TournamentFinalGroupAdmin(KorfbalModelAdmin):
    """Give bracket groups their event and format context."""

    list_display = ("name", "tournament", "format", "sort_order")
    search_fields = ("id_uuid", "name", "tournament__name")
    list_filter = (relation_filter("tournament", "Tournament"), "format")
    list_select_related = ("tournament",)


@admin.register(TournamentStage)
class TournamentStageAdmin(KorfbalModelAdmin):
    """Distinguish pool and bracket stages with searchable labels."""

    list_display = ("name", "tournament", "final_group", "kind", "sort_order")
    search_fields = ("id_uuid", "name", "tournament__name", "final_group__name")
    list_filter = (relation_filter("tournament", "Tournament"), "kind")
    list_select_related = ("tournament", "final_group__tournament")


@admin.register(TournamentPool)
class TournamentPoolAdmin(KorfbalModelAdmin):
    """Show each pool's stage and assigned field."""

    list_display = ("name", "tournament", "stage", "assigned_field", "sort_order")
    search_fields = ("id_uuid", "name", "tournament__name", "stage__name")
    list_filter = (relation_filter("tournament", "Tournament"),)
    list_select_related = (
        "tournament",
        "stage__tournament",
        "assigned_field__tournament",
    )


@admin.register(TournamentPoolEntry)
class TournamentPoolEntryAdmin(KorfbalModelAdmin):
    """Find and order individual pool entries."""

    list_display = ("team", "pool", "seed_order")
    search_fields = ("id_uuid", "team__name", "pool__name", "pool__tournament__name")
    list_filter = (relation_filter("pool", "Pool"),)
    list_select_related = ("team", "pool__tournament")


@admin.register(TournamentMatch)
class TournamentMatchAdmin(KorfbalModelAdmin):
    """Read match status and scores without opening every row."""

    list_display = (
        "match_number",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "status",
        "starts_at",
        "tournament",
        "stage",
        "field",
    )
    search_fields = (
        "id_uuid",
        "tournament__name",
        "home_team__name",
        "away_team__name",
        "referee_name",
    )
    list_filter = (relation_filter("tournament", "Tournament"), "status", "starts_at")
    list_select_related = (
        "tournament",
        "stage__tournament",
        "pool__tournament",
        "home_team",
        "away_team",
        "field__tournament",
        "winner",
        "next_match__home_team",
        "next_match__away_team",
        "referee_team",
        "referee_player__user",
        "field_ready_by",
    )
    ordering = ("-starts_at", "match_number")
    readonly_fields = ("revision", "created_at", "updated_at")
    fieldsets = (
        (
            "Fixture",
            {
                "fields": (
                    "tournament",
                    "stage",
                    "pool",
                    "match_number",
                    "round_number",
                    "home_team",
                    "away_team",
                )
            },
        ),
        (
            "Schedule and result",
            {
                "fields": (
                    "starts_at",
                    "duration_minutes",
                    "field",
                    "status",
                    "home_score",
                    "away_score",
                    "winner",
                )
            },
        ),
        (
            "Officials",
            {
                "classes": ("collapse",),
                "fields": (
                    "referee_team",
                    "referee_name",
                    "referee_player",
                    "referee_claimed_at",
                    "field_ready_at",
                    "field_ready_by",
                    "field_ready_by_name",
                ),
            },
        ),
        (
            "Bracket progression",
            {
                "classes": ("collapse",),
                "fields": (
                    "home_qualifier",
                    "away_qualifier",
                    "next_match",
                    "winner_to_side",
                ),
            },
        ),
        (
            "Record information",
            {
                "classes": ("collapse",),
                "fields": ("id_uuid", "revision", "created_at", "updated_at"),
            },
        ),
    )


@admin.register(TournamentMember)
class TournamentMemberAdmin(KorfbalModelAdmin):
    """Locate organizer and scorekeeper assignments."""

    list_display = ("user", "tournament", "role", "field")
    search_fields = ("user__username", "user__email", "tournament__name")
    list_filter = (relation_filter("tournament", "Tournament"), "role")
    list_select_related = ("user", "tournament", "field__tournament")


@admin.register(TournamentStandingAdjustment)
class TournamentStandingAdjustmentAdmin(KorfbalModelAdmin):
    """Make the reason and author of a standings adjustment visible."""

    list_display = ("entry", "points", "reason", "created_by", "created_at")
    search_fields = (
        "id_uuid",
        "entry__team__name",
        "entry__pool__tournament__name",
        "reason",
    )
    list_filter = ("created_at",)
    list_select_related = ("entry__team", "entry__pool__tournament", "created_by")
    ordering = ("-created_at",)


@admin.register(TournamentResultAudit)
class TournamentResultAuditAdmin(KorfbalModelAdmin):
    """Expose result transitions and their attribution in one list."""

    list_display = (
        "match",
        "previous_status",
        "new_status",
        "new_home_score",
        "new_away_score",
        "source",
        "changed_by",
        "created_at",
    )
    search_fields = (
        "id_uuid",
        "match__id_uuid",
        "match__tournament__name",
        "match__home_team__name",
        "match__away_team__name",
        "reason",
    )
    list_filter = ("source", "created_at")
    list_select_related = ("match__home_team", "match__away_team", "changed_by")
    ordering = ("-created_at",)


@admin.register(TournamentDisplayConfig)
class TournamentDisplayConfigAdmin(KorfbalModelAdmin):
    """Review public display settings without opening unrelated event records."""

    list_display = (
        "tournament",
        "rotation_seconds",
        "show_live",
        "show_standings",
        "show_sponsors",
    )
    search_fields = ("tournament__name", "tournament__id_uuid", "announcement")
    list_filter = ("show_live", "show_sponsors")
    list_select_related = ("tournament",)
