"""Platform review of club join requests and club claims."""

from typing import TYPE_CHECKING

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.club.composition import join_request_ports
from apps.club.models import ClubJoinRequest
from apps.club.services.join_requests import JoinRequestError, decide_request
from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.player.models import Player


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    JoinRequestAdminBase = ModelAdminBase[ClubJoinRequest]
else:
    JoinRequestAdminBase = KorfbalModelAdmin


@admin.register(ClubJoinRequest)
class ClubJoinRequestAdmin(JoinRequestAdminBase):
    """Decide club claims (and member requests for clubs without an admin)."""

    list_display = (
        "player",
        "club",
        "kind",
        "status",
        "knkv_person_id",
        "link_status",
        "created_at",
    )
    list_filter = ("kind", "status", "link_status")
    search_fields = ("club__name", "player__name", "player__user__username")
    list_select_related = ("player__user", "club")
    readonly_fields = (
        "player",
        "club",
        "team",
        "kind",
        "status",
        "knkv_person_id",
        "note",
        "link_status",
        "link_error",
        "created_at",
        "decided_at",
        "decided_by",
    )
    actions = ("approve", "reject")

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse manual creation; requests come from the setup flow only."""
        return False

    def _decide(
        self,
        request: HttpRequest,
        queryset: QuerySet[ClubJoinRequest],
        *,
        approve: bool,
    ) -> None:
        reviewer = Player.objects.filter(user=request.user).first()
        decided = 0
        for join_request in queryset:
            try:
                decide_request(
                    request_id=str(join_request.pk),
                    club=None,
                    approve=approve,
                    reviewer=reviewer,
                    ports=join_request_ports,
                )
            except JoinRequestError as exc:
                self.message_user(
                    request, f"{join_request}: {exc}", level=messages.WARNING
                )
            else:
                decided += 1
        self.message_user(request, f"{decided} aanmelding(en) verwerkt.")

    # Deciding grants club admin rights or links a KNKV identity: viewing the
    # requests is not enough.
    @admin.action(description="Accepteren", permissions=["change"])
    def approve(
        self, request: HttpRequest, queryset: QuerySet[ClubJoinRequest]
    ) -> None:
        """Approve the selected pending requests."""
        self._decide(request, queryset, approve=True)

    @admin.action(description="Afwijzen", permissions=["change"])
    def reject(self, request: HttpRequest, queryset: QuerySet[ClubJoinRequest]) -> None:
        """Reject the selected pending requests."""
        self._decide(request, queryset, approve=False)
