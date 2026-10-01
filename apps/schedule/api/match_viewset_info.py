"""MatchViewSet action for venue, rules and kit information."""

from __future__ import annotations

from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.competition.queries.match_info import match_info

from .match_viewset_contracts import MatchViewSetContext


class MatchInfoActionsMixin:
    """Schedule action for KNKV match information."""

    @action(detail=True, methods=("GET",), url_path="info")
    def info(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return the venue, competition rules and both clubs' kits.

        Returns:
            Response: Normalized KNKV information; empty for unlinked matches.

        """
        return Response(match_info(self.get_object()))
