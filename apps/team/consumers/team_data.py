"""This module contains the TeamDataConsumer class that handles the websocket connection for the team data page."""  # noqa: E501

import json
import traceback
from datetime import datetime

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q

from apps.common.utils import general_stats, players_stats, transform_matchdata
from apps.game_tracker.models import MatchData, MatchPart
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData
from apps.common.utils import get_time


class TeamDataConsumer(AsyncWebsocketConsumer):
    """Websocket consumer for the team data page."""

    def __init__(self):
        """Initialize the TeamDataConsumer."""
        super().__init__()
        self.team = None

    async def connect(self):
        """Connect to the websocket."""
        team_id = self.scope["url_route"]["kwargs"]["id"]
        self.team = await Team.objects.aget(id_uuid=team_id)
        self.subscribed_channels = []
        await self.accept()

    async def receive(self, text_data=None, bytes_data=None) -> None:
        """
        Receive the data from the websocket.

        Args:
            text_data (str): The data received from the websocket.
            bytes_data (bytes): The bytes data received from the websocket.

        Raises:
            Exception: If an error occurs while processing the data.
        """
        try:
            json_data = json.loads(text_data)
            command = json_data["command"]

            if command == "wedstrijden" or command == "ended_matches":
                await self.matches_request(command)

            elif command == "get_stats":
                data_type = json_data["data_type"]

                if data_type == "general":
                    await self.team_stats_general_request()

                elif data_type == "player_stats":
                    await self.team_stats_player_request()

            elif command == "spelers":
                await self.player_request(json_data)

            elif command == "follow":
                await self.follow_request(json_data["followed"], json_data["user_id"])

            elif command == "get_time":
                match_data = await MatchData.objects.prefetch_related(
                    "match_link"
                ).aget(id_uuid=json_data["match_data_id"])

                current_part = await MatchPart.objects.aget(
                    match_data=match_data, active=True
                )

                # Subscribe to time data channel
                if match_data.match_link.id_uuid not in self.subscribed_channels:
                    await self.channel_layer.group_add(
                        f"time_match_{match_data.match_link.id_uuid}", self.channel_name
                    )

                    self.subscribed_channels.append(match_data.match_link.id_uuid)

                await self.send(
                    text_data=await get_time(match_data, current_part)
                )

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {"error": str(e), "traceback": traceback.format_exc()}
                )
            )

    async def matches_request(self, command: str) -> None:
        """
        Handle the request for matches.

        Args:
            command (str): The command to determine the type of matches to fetch.
        """
        wedstrijden_data = await self.get_matchs_data(
            ["upcoming", "active"] if command == "wedstrijden" else ["finished"],
            "" if command == "wedstrijden" else "-",
        )

        wedstrijden_dict = await transform_matchdata(wedstrijden_data)

        await self.send(
            text_data=json.dumps(
                {"command": "wedstrijden", "wedstrijden": wedstrijden_dict}
            )
        )

    async def team_stats_general_request(self) -> None:
        """Handle the request for general team statistics."""
        # get a list of all the matches of the team
        matches = await sync_to_async(list)(
            Match.objects.filter(Q(home_team=self.team) | Q(away_team=self.team))
        )

        match_datas = await sync_to_async(list)(
            MatchData.objects.prefetch_related(
                "match_link",
                "match_link__home_team",
                "match_link__away_team",
            ).filter(match_link__in=matches)
        )

        general_stats_json = await general_stats(match_datas)

        await self.send(text_data=general_stats_json)

    async def team_stats_player_request(self) -> None:
        """Handle the request for player statistics."""
        # Fetch players and matches in bulk
        players = await sync_to_async(list)(
            Player.objects.prefetch_related("user")
            .filter(team_data_as_player__team=self.team)
            .distinct()
        )

        matches = await sync_to_async(list)(
            Match.objects.filter(Q(home_team=self.team) | Q(away_team=self.team))
        )

        match_datas = await sync_to_async(list)(
            MatchData.objects.filter(match_link__in=matches)
        )

        player_stats = await players_stats(players, match_datas)

        # Prepare and send data
        await self.send(text_data=player_stats)

    async def player_request(self, json_data: dict) -> None:
        """
        Handle the request for players.

        Args:
            json_data (dict): The JSON data containing the request.
        """
        if "season_uuid" in json_data:
            season_uuid = json_data["season_uuid"]
        else:
            season_uuid = None

        # Initialize an empty list to store players
        players_in_team_season = []

        # Check if a specific season is provided
        if season_uuid:
            # Assuming you have a Season object or its UUID
            season = await Season.objects.aget(id_uuid=season_uuid)

            # Get all TeamData instances for the specified team and season
            team_data_instances = await sync_to_async(list)(
                TeamData.objects.prefetch_related("players").filter(
                    team=self.team, season=season
                )
            )

            # Iterate through the TeamData instances and collect players
            for team_data_instance in team_data_instances:
                all_players = await sync_to_async(team_data_instance.players.all)()
                players_prefetch = await sync_to_async(all_players.prefetch_related)(
                    "user"
                )
                await sync_to_async(players_in_team_season.extend)(players_prefetch)
        else:
            # retreve the players of the current season or
            # last season if there is no current season
            try:
                current_season = await Season.objects.aget(
                    start_date__lte=datetime.now(), end_date__gte=datetime.now()
                )
            except Season.DoesNotExist:
                current_season = (
                    await Season.objects.filter(end_date__lte=datetime.now())
                    .order_by("-end_date")
                    .afirst()
                )

            # Get the team data instances for the current season
            all_team_data_instances = await sync_to_async(list)(
                TeamData.objects.prefetch_related("players").filter(
                    team=self.team, season=current_season
                )
            )

            # Iterate through all TeamData instances and collect players
            for team_data_instance in all_team_data_instances:
                all_players = await sync_to_async(team_data_instance.players.all)()
                players_prefetch = await sync_to_async(all_players.prefetch_related)(
                    "user"
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
                    "command": "spelers",
                    "spelers": players_in_team_season_dict,
                }
            )
        )

    async def follow_request(self, follow: bool, user_id: str) -> None:
        """
        Handle the request to follow or unfollow the team.

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
            text_data=json.dumps({"command": "follow", "status": "success"})
        )

    async def get_matchs_data(self, status: list, order: str) -> list:
        """
        Get the match data of the team.

        Args:
            status (list): The status of the matches.
            order (str): The order of the matches.

        Returns:
            list: The list of match data.
        """
        matches = await sync_to_async(list)(
            Match.objects.filter(
                Q(home_team=self.team) | Q(away_team=self.team)
            ).distinct()
        )

        matches_non_dub = list(dict.fromkeys(matches))

        matchs_data = await sync_to_async(list)(
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
            .order_by(order + "match_link__start_time")
        )

        return matchs_data

    async def send_data(self, event):
        """
        Send data to the websocket.

        Args:
            event: The event to send.
        """
        data = event["data"]
        await self.send(text_data=json.dumps(data))