"""Public tournament views, grouped by endpoint responsibility."""

from .common import Conflict
from .finals import (
    TournamentFinalGroupDetailView,
    TournamentFinalGroupListCreateView,
    TournamentFinalsGenerateView,
)
from .planning import (
    TournamentGenerationApplyView,
    TournamentGenerationPreviewView,
    TournamentMatchDetailView,
    TournamentMatchesGenerateView,
    TournamentMatchListCreateView,
    TournamentPoolDetailView,
    TournamentPoolListCreateView,
    TournamentPoolsGenerateView,
    TournamentScheduleImportView,
)
from .referee_access import (
    TournamentRefereeAssignmentView,
    TournamentRefereeClaimView,
    TournamentRefereeDutiesView,
    TournamentRefereePdfView,
    TournamentRefereeQrView,
)
from .referee_tracker import (
    TournamentRefereeGoalView,
    TournamentRefereeLatestEventView,
    TournamentRefereeReadyView,
    TournamentRefereeTrackerView,
)
from .resources import (
    TournamentFieldDetailView,
    TournamentFieldListCreateView,
    TournamentMemberDetailView,
    TournamentMemberListCreateView,
    TournamentStandingAdjustmentDetailView,
    TournamentStandingAdjustmentListCreateView,
    TournamentTeamDetailView,
    TournamentTeamListCreateView,
    TournamentTeamSubstitutionView,
)
from .results import (
    TournamentMatchReadinessView,
    TournamentMatchResultView,
    TournamentMatchStateResetView,
    TournamentRoundStartView,
)
from .tournaments import (
    TournamentDisplayConfigView,
    TournamentPublicView,
    TournamentPublishView,
    TournamentSnapshotView,
    TournamentViewSet,
)


__all__ = [
    "Conflict",
    "TournamentDisplayConfigView",
    "TournamentFieldDetailView",
    "TournamentFieldListCreateView",
    "TournamentFinalGroupDetailView",
    "TournamentFinalGroupListCreateView",
    "TournamentFinalsGenerateView",
    "TournamentGenerationApplyView",
    "TournamentGenerationPreviewView",
    "TournamentMatchDetailView",
    "TournamentMatchListCreateView",
    "TournamentMatchReadinessView",
    "TournamentMatchResultView",
    "TournamentMatchStateResetView",
    "TournamentMatchesGenerateView",
    "TournamentMemberDetailView",
    "TournamentMemberListCreateView",
    "TournamentPoolDetailView",
    "TournamentPoolListCreateView",
    "TournamentPoolsGenerateView",
    "TournamentPublicView",
    "TournamentPublishView",
    "TournamentRefereeAssignmentView",
    "TournamentRefereeClaimView",
    "TournamentRefereeDutiesView",
    "TournamentRefereeGoalView",
    "TournamentRefereeLatestEventView",
    "TournamentRefereePdfView",
    "TournamentRefereeQrView",
    "TournamentRefereeReadyView",
    "TournamentRefereeTrackerView",
    "TournamentRoundStartView",
    "TournamentScheduleImportView",
    "TournamentSnapshotView",
    "TournamentStandingAdjustmentDetailView",
    "TournamentStandingAdjustmentListCreateView",
    "TournamentTeamDetailView",
    "TournamentTeamListCreateView",
    "TournamentTeamSubstitutionView",
    "TournamentViewSet",
]
