"""Consumers for the club app."""

import json
from typing import Any, Dict, List, Optional, TypedDict

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q

from apps.common.utils import get_time_display_pause, transform_match_data
from apps.game_tracker.models import MatchData
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team


class TeamJSON(TypedDict):
    """Type definition for the team JSON data."""

    id: str
    name: str
    logo: str
    get_absolute_url: str


class ClubDataConsumer(AsyncWebsocketConsumer):
    """Websocket consumer for the club data."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the ClubDataConsumer."""
        super().__init__(*args, **kwargs)
        self.player: Optional[Player] = None
        self.club: Optional[str] = None
        self.subscribed_channels: List[str] = []

    async def connect(self) -> None:
        """Connect the websocket."""
        self.club = self.scope["url_route"]["kwargs"]["id"]
        await self.accept()

    async def receive(
        self, text_data: Optional[str] = None, bytes_data: Optional[bytes] = None
    ) -> None:
        """
        Receive data from the websocket.

        Args:
            text_data: The received text data.
            bytes_data: The received bytes data.
        """
        if text_data is None:
            await self.send(text_data=json.dumps({"error": "No data received"}))
            return

        try:
            json_data: Dict[str, Any] = json.loads(text_data)
            command: str = json_data["command"]
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({"error": "Invalid JSON"}))
            return

        if command == "teams":
            await self.teams_request()
        elif command in {"matches", "ended_matches"}:
            await self.matches_request(command)
        elif command == "follow":
            await self.follow_request(json_data["followed"], json_data["user_id"])
        elif command == "get_time":
            await get_time_display_pause(self, json_data)

    async def teams_request(self) -> None:
        """Send the teams data to the client."""
        teams: List[Team] = await sync_to_async(list)(
            Team.objects.filter(club=self.club)
        )
        teams = list(dict.fromkeys(teams))  # Remove duplicates

        teams_json: List[TeamJSON] = [
            {
                "id": str(team.id_uuid),
                "name": await sync_to_async(team.__str__)(),
                "logo": team.club.get_club_logo(),
                "get_absolute_url": str(team.get_absolute_url()),
            }
            for team in teams
        ]

        await self.send(text_data=json.dumps({"command": "teams", "teams": teams_json}))

    async def matches_request(self, command: str) -> None:
        """
        Send the match data to the client.

        Args:
            command: The command to determine which matches to send.
        """
        teams: List[Team] = await sync_to_async(list)(
            Team.objects.filter(club=self.club)
        )
        team_ids: List[str] = [str(team.id_uuid) for team in teams]

        status: List[str]
        order: str
        if command == "matches":
            status = ["upcoming", "active"]
            order = ""
        else:
            status = ["finished"]
            order = "-"

        matches_data: List[MatchData] = await self.get_matches_data(
            team_ids, status, order
        )
        matches_dict = await transform_match_data(matches_data)

        await self.send(
            text_data=json.dumps({"command": "matches", "matches": matches_dict})
        )

    async def get_matches_data(
        self, team_ids: List[str], status: List[str], order: str
    ) -> List[MatchData]:
        """
        Get the match data for the given teams and status.

        Args:
            team_ids: The ids of the teams.
            status: The status of the matches.
            order: The order of the matches.

        Returns:
            The match data for the given teams and status.
        """
        matches: List[Match] = await sync_to_async(list)(
            Match.objects.filter(
                Q(home_team__in=team_ids) | Q(away_team__in=team_ids)
            ).distinct()
        )
        matches_non_dub: List[Match] = list(dict.fromkeys(matches))

        matches_data: List[MatchData] = await sync_to_async(list)(
            MatchData.objects.prefetch_related(
                "match_link",
                "match_link__home_team",
                "match_link__home_team__club",
                "match_link__away_team",
                "match_link__away_team__club",
            )
            .filter(match_link__in=matches_non_dub, status__in=status)
            .order_by(order + "match_link__start_time")
        )

        return matches_data

    async def follow_request(self, follow: bool, user_id: str) -> None:
        """
        Handle the follow request.

        Args:
            follow: Whether the user wants to follow the club.
            user_id: The id of the user.
        """
        player: Player = await sync_to_async(Player.objects.get)(user=user_id)

        if follow:
            await sync_to_async(player.club_follow.add)(self.club)
        else:
            await sync_to_async(player.club_follow.remove)(self.club)

        await self.send(
            text_data=json.dumps({"command": "follow", "status": "success"})
        )

    async def send_data(self, event):
        """
        Send data to the websocket.

        Args:
            event: The event to send.
        """
        data = event["data"]
        await self.send(text_data=json.dumps(data))
