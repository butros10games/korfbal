"""Module contains the ProfileDataConsumer class."""

import json
import traceback

from asgiref.sync import sync_to_async
from bg_auth.models import UserProfile
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from django.db.models import Q

from apps.common.utils import get_time_display_pause, transform_match_data
from apps.game_tracker.models import GoalType, MatchData, Shot
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


class ProfileDataConsumer(AsyncWebsocketConsumer):
    """Websocket consumer for the profile data."""

    def __init__(self) -> None:
        """Initialize the ProfileDataConsumer."""
        super().__init__()
        self.user: User = User()
        self.player: Player = Player()
        self.user_profile: UserProfile = UserProfile()
        self.teams: list[Team] = []
        self.subscribed_channels: list[str] = []

    async def connect(self) -> None:
        """Connect to the websocket."""
        player_id = self.scope["url_route"]["kwargs"]["id"]
        self.player = await Player.objects.prefetch_related("user").aget(
            id_uuid=player_id
        )
        self.user = self.player.user
        self.user_profile = await UserProfile.objects.aget(user=self.user)
        self.teams = await sync_to_async(list)(
            Team.objects.filter(team_data__players=self.player).distinct()
        )

        self.team_data = await sync_to_async(
            TeamData.objects.filter(
                Q(players=self.player) | Q(coach=self.player)
            ).distinct
        )()
        await self.accept()

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        """Receive the data from the websocket.

        Args:
            text_data (str): The text data.
            bytes_data (bytes): The bytes data.

        """
        if text_data is None:
            return

        try:
            json_data = json.loads(text_data)
            command = json_data["command"]

            if command == "player_stats":
                await self.player_stats_request()

            if command == "settings_request":
                if not self.user or not self.user_profile:
                    raise ValueError("User or UserProfile not found.")

                await self.send(
                    text_data=json.dumps(
                        {
                            "command": "settings_request",
                            "username": self.user.username,
                            "email": self.user.email,
                            "first_name": self.user.first_name,
                            "last_name": self.user.last_name,
                            "email_2fa": self.user_profile.email_2fa,
                        }
                    )
                )

            if command == "settings_update":
                await self.settings_update_request(json_data["data"])

            if command == "update_profile_picture_url":
                await self.settings_update_request(json_data["url"])

            if command == "teams":
                await self.teams_request()

            if command == "upcoming_matches" or command == "past_matches":
                await self.matches_request(command)

            elif command == "get_time":
                await get_time_display_pause(self, json_data)

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {"error": str(e), "traceback": traceback.format_exc()}
                )
            )

    async def player_stats_request(self) -> None:
        """Send the player stats to the client."""
        total_goals_for = 0
        total_goals_against = 0

        all_finished_match_data = await self.get_matches_data(["finished"], "-")

        goal_types = await sync_to_async(list)(GoalType.objects.all())

        player_goal_stats = {}
        scoring_types = []

        for goal_type in goal_types:
            goals_by_player = await Shot.objects.filter(
                match_data__in=all_finished_match_data,
                shot_type=goal_type,
                player=self.player,
                for_team=True,
                scored=True,
            ).acount()

            goals_against_player = await Shot.objects.filter(
                match_data__in=all_finished_match_data,
                shot_type=goal_type,
                player=self.player,
                for_team=False,
                scored=True,
            ).acount()

            player_goal_stats[goal_type.name] = {
                "goals_by_player": goals_by_player,
                "goals_against_player": goals_against_player,
            }

            total_goals_for += goals_by_player
            total_goals_against += goals_against_player

            scoring_types.append(goal_type.name)

        await self.send(
            text_data=json.dumps(
                {
                    "command": "player_goal_stats",
                    "player_goal_stats": player_goal_stats,
                    "scoring_types": scoring_types,
                    "played_matches": len(all_finished_match_data),
                    "total_goals_for": total_goals_for,
                    "total_goals_against": total_goals_against,
                }
            )
        )

    async def settings_update_request(self, data: dict) -> None:
        """Update the user settings.

        Args:
            data (dict): The data to update.

        """
        username = data["username"]
        email = data["email"]
        first_name = data["first_name"]
        last_name = data["last_name"]
        email_2fa = data["email_2fa"]

        self.user.username = username
        self.user.email = email
        self.user.first_name = first_name
        self.user.last_name = last_name
        await self.user.asave()

        self.user_profile.email_2fa = email_2fa
        await self.user_profile.asave()

        await self.send(text_data=json.dumps({"command": "settings_updated"}))

    async def update_profile_picture_url_request(self, url: str) -> None:
        """Update the profile picture URL.

        Args:
            url (str): The URL of the profile picture.

        """
        if url:
            # Assuming "url" contains the relative path of the image
            self.player.profile_picture = url
            await self.player.asave()

            # Send a response back to the client if needed
            await self.send(
                text_data=json.dumps(
                    {"command": "profile_picture_updated", "status": "success"}
                )
            )

    async def teams_request(self) -> None:
        """Send the teams to the client."""
        teams_dict = [
            {
                "id": str(team.id_uuid),
                "name": await sync_to_async(team.__str__)(),
                "logo": team.club.get_club_logo(),
                "get_absolute_url": str(team.get_absolute_url()),
            }
            for team in self.teams
        ]

        await self.send(text_data=json.dumps({"command": "teams", "teams": teams_dict}))

    async def matches_request(self, command: str) -> None:
        """Send the matches to the client.

        Args:
            command (str): The command to get the matches.

        """
        matches_data = await self.get_matches_data(
            ["upcoming", "active"] if command == "upcoming_matches" else ["finished"],
            "" if command == "upcoming_matches" else "-",
        )

        matches_dict = await transform_match_data(matches_data)

        await self.send(
            text_data=json.dumps({"command": "matches", "matches": matches_dict})
        )

    async def get_matches_data(self, status: list, order: str) -> list:
        """Get the match data.

        Args:
            status (list): The status of the match data.
            order (str): The order of the match data.

        Returns:
            list: The list of match data.

        """
        matches = await sync_to_async(list)(
            Match.objects.filter(
                Q(home_team__team_data__in=self.team_data)
                | Q(away_team__team_data__in=self.team_data)
            ).distinct()
        )

        matches_non_dub = list(dict.fromkeys(matches))

        matches_data = await sync_to_async(list)(
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

    async def send_data(self, event: dict) -> None:
        """Send data to the websocket.

        Args:
            event: The event to send.

        """
        data = event["data"]
        await self.send(text_data=json.dumps(data))
