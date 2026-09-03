"""Django admin registrations for tournament operations."""

from django.contrib import admin

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


admin.site.register(Tournament)
admin.site.register(TournamentDisplayConfig)
admin.site.register(TournamentField)
admin.site.register(TournamentFinalGroup)
admin.site.register(TournamentMatch)
admin.site.register(TournamentMember)
admin.site.register(TournamentPool)
admin.site.register(TournamentPoolEntry)
admin.site.register(TournamentResultAudit)
admin.site.register(TournamentStage)
admin.site.register(TournamentStandingAdjustment)
admin.site.register(TournamentTeam)
