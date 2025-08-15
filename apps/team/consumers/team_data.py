"""Module contains TeamDataConsumer class that handles websocket connection for team
data page.
"""

import json

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q
from django.utils import timezone

from apps.game_tracker.models import MatchData
from apps.kwt_common.utils import (
    general_stats,
    get_time_display_pause,
    players_stats,
    transform_match_data,
)
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


class TeamDataConsumer(AsyncWebsocketConsumer):
    """Websocket consumer for the team data page."""

    def __init__(self) -> None:
        """Initialize the TeamDataConsumer."""
        super().__init__()
        self.team = None

    async def connect(self) -> None:
        """Connect to the websocket."""
        team_id = self.scope["url_route"]["kwargs"]["id"]
        self.team = await Team.objects.aget(id_uuid=team_id)
        self.subscribed_channels = []
        await self.accept()

    async def receive(
        self,
        text_data: str | None = None,
        bytes_data: bytes | None = None,
    ) -> None:
        """Receive the data from the websocket.

        Args:
            text_data (str): The data received from the websocket.
            bytes_data (bytes): The bytes data received from the websocket.

        """
        if bytes_data:
            text_data = bytes_data.decode("utf-8")
        if text_data is None:
            await self.send(text_data=json.dumps({"error": "No data received"}))
            return

        json_data = json.loads(text_data)
        command = json_data["command"]

        match command:
            case "matches", "ended_matches":
                await self.matches_request(json_data["command"])

            case "get_stats":
                data_type = json_data["data_type"]

                if data_type == "general":
                    await self.team_stats_general_request()

                elif data_type == "player_stats":
                    await self.team_stats_player_request()

            case "players":
                await self.player_request(json_data)

            case "follow":
                await self.follow_request(json_data["followed"], json_data["user_id"])

            case "get_time":
                await get_time_display_pause(self, json_data)

    async def matches_request(self, command: str) -> None:
        """Handle the request for matches.

        Args:
            command (str): The command to determine the type of matches to fetch.

        """
        matches_data = await self.get_matches_data(
            ["upcoming", "active"] if command == "matches" else ["finished"],
            "" if command == "matches" else "-",
        )

        matches_dict = await transform_match_data(matches_data)

        await self.send(
            text_data=json.dumps({"command": "matches", "matches": matches_dict}),
        )

    async def team_stats_general_request(self) -> None:
        """Handle the request for general team statistics."""
        # get a list of all the matches of the team
        matches = await sync_to_async(list)(
            Match.objects.filter(Q(home_team=self.team) | Q(away_team=self.team)),
        )

        match_dataset = await sync_to_async(list)(
            MatchData.objects.prefetch_related(
                "match_link",
                "match_link__home_team",
                "match_link__away_team",
            ).filter(match_link__in=matches),
        )

        general_stats_json = await general_stats(match_dataset)

        await self.send(text_data=general_stats_json)

    async def team_stats_player_request(self) -> None:
        """Handle the request for player statistics."""
        # Fetch players and matches in bulk
        players = await sync_to_async(list)(
            Player.objects.prefetch_related("user")
            .filter(team_data_as_player__team=self.team)
            .distinct(),
        )

        matches = await sync_to_async(list)(
            Match.objects.filter(Q(home_team=self.team) | Q(away_team=self.team)),
        )

        match_dataset = await sync_to_async(list)(
            MatchData.objects.filter(match_link__in=matches),
        )

        player_stats = await players_stats(players, match_dataset)

        # Prepare and send data
        await self.send(text_data=player_stats)

    async def player_request(self, json_data: dict) -> None:
        """Handle the request for players.

        Args:
            json_data (dict): The JSON data containing the request.

        """
        season_uuid = json_data.get("season_uuid")

        # Initialize an empty list to store players
        players_in_team_season = []

        # Check if a specific season is provided
        if season_uuid:
            # Assuming you have a Season object or its UUID
            season = await Season.objects.aget(id_uuid=season_uuid)

            # Get all TeamData instances for the specified team and season
            team_data_instances = await sync_to_async(list)(
                TeamData.objects.prefetch_related("players").filter(
                    team=self.team,
                    season=season,
                ),
            )

            # Iterate through the TeamData instances and collect players
            for team_data_instance in team_data_instances:
                all_players = await sync_to_async(team_data_instance.players.all)()
                players_prefetch = await sync_to_async(all_players.prefetch_related)(
                    "user",
                )
                await sync_to_async(players_in_team_season.extend)(players_prefetch)
        else:
            # retrieve the players of the current season or
            # last season if there is no current season
            try:
                current_season = await Season.objects.aget(
                    start_date__lte=timezone.now().date(),
                    end_date__gte=timezone.now().date(),
                )
            except Season.DoesNotExist:
                current_season = (
                    await Season.objects.filter(end_date__lte=timezone.now().date())
                    .order_by("-end_date")
                    .afirst()
                )

            # Get the team data instances for the current season
            all_team_data_instances = await sync_to_async(list)(
                TeamData.objects.prefetch_related("players").filter(
                    team=self.team,
                    season=current_season,
                ),
            )

            # Iterate through all TeamData instances and collect players
            for team_data_instance in all_team_data_instances:
                all_players = await sync_to_async(team_data_instance.players.all)()
                players_prefetch = await sync_to_async(all_players.prefetch_related)(
                    "user",
                )
                await sync_to_async(players_in_team_season.extend)(players_prefetch)

        players_in_team_season_list = await sync_to_async(list)(players_in_team_season)

        players_in_team_season_dict = [
            {
                "id": str(player.id_uuid),
                "name": player.user.username,
                "profile_picture": player.get_profile_picture(),
                "get_absolute_url": str(player.get_absolute_url()),
            }
            for player in players_in_team_season_list
        ]

        await self.send(
            text_data=json.dumps(
                {
                    "command": "players",
                    "players": players_in_team_season_dict,
                },
            ),
        )

    async def follow_request(self, follow: bool, user_id: str) -> None:
        """Handle the request to follow or unfollow the team.

        Args:
            follow (bool): The status of the follow request.
            user_id (str): The UUID of the user.

        """
        player = await Player.objects.aget(user=user_id)

        if follow:
            await player.team_follow.aadd(self.team)

        else:
            await player.team_follow.aremove(self.team)

        await self.send(
            text_data=json.dumps({"command": "follow", "status": "success"}),
        )

    async def get_matches_data(self, status: list, order: str) -> list:
        """Get the match data of the team.

        Args:
            status (list): The status of the matches.
            order (str): The order of the matches.

        Returns:
            list: The list of match data.

        """
        matches = await sync_to_async(list)(
            Match.objects.filter(
                Q(home_team=self.team) | Q(away_team=self.team),
            ).distinct(),
        )

        matches_non_dub = list(dict.fromkeys(matches))

        return await sync_to_async(list)(
            MatchData.objects.prefetch_related(
                "match_link",
                "match_link__home_team",
                "match_link__home_team__club",
                "match_link__away_team",
                "match_link__away_team__club",
            )
            .filter(
                match_link__in=matches_non_dub,
                status__in=status,
            )
            .order_by(order + "match_link__start_time"),
        )

    async def send_data(self, event: dict) -> None:
        """Send data to the websocket.

        Args:
            event: The event to send.

        """
        data = event["data"]
        await self.send(text_data=json.dumps(data))
