from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q

from apps.game_tracker.models import Shot, GoalType, MatchData
from apps.schedule.models import Match
from apps.player.models import Player
from apps.team.models import Team, TeamData
from authentication.models import UserProfile

from apps.common.utils import transform_matchdata

import json
import traceback


class ProfileDataConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.player = None
        self.user_profile = None
        self.teams = None

    async def connect(self):
        player_id = self.scope["url_route"]["kwargs"]["id"]
        self.player = await Player.objects.prefetch_related(
            "user"
        ).aget(id_uuid=player_id)
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

    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data["command"]

            if command == "player_stats":
                await self.player_stats_request()

            if command == "settings_request":
                await self.send(text_data=json.dumps({
                    "command": "settings_request",
                    "username": self.user.username,
                    "email": self.user.email,
                    "first_name": self.user.first_name,
                    "last_name": self.user.last_name,
                    "email_2fa": self.user_profile.email_2fa
                }))

            if command == "settings_update":
                await self.settings_update_request(json_data["data"])

            if command == "update_profile_picture_url":
                await self.settings_update_request(json_data["url"])
 
            if command == "teams":
               await self.teams_request()

            if command == "upcomming_matches" or command == "past_matches":
                await self.matches_request(command)

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {"error": str(e), "traceback": traceback.format_exc()}
                )
            )

    async def player_stats_request(self):
        total_goals_for = 0
        total_goals_against = 0

        all_finished_match_data = await self.get_matchs_data(["finished"], "-")

        goal_types = await sync_to_async(list)(GoalType.objects.all())

        player_goal_stats = {}
        scoring_types = []

        for goal_type in goal_types:
            goals_by_player = await Shot.objects.filter(
                match_data__in=all_finished_match_data, 
                shot_type=goal_type, 
                player=self.player,
                for_team=True,
                scored=True
            ).acount()

            goals_against_player = await Shot.objects.filter(
                match_data__in=all_finished_match_data, 
                shot_type=goal_type, 
                player=self.player,
                for_team=False,
                scored=True
            ).acount()

            player_goal_stats[goal_type.name] = {
                "goals_by_player": goals_by_player,
                "goals_against_player": goals_against_player
            }

            total_goals_for += goals_by_player
            total_goals_against += goals_against_player

            scoring_types.append(goal_type.name)

        await self.send(text_data=json.dumps({
            "command": "player_goal_stats",
            "player_goal_stats": player_goal_stats,
            "scoring_types": scoring_types,
            "played_matches": len(all_finished_match_data),
            "total_goals_for": total_goals_for,
            "total_goals_against": total_goals_against,
        }))

    async def settings_update_request(self, data):
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

        await self.send(
            text_data=json.dumps(
                {"command": "settings_updated"}
            )
        )

    async def update_profile_picture_url_request(self, url):
        if url:
            self.player.profile_picture = url  # Assuming "url" contains the relative path of the image
            await self.player.asave()

            # Send a response back to the client if needed
            await self.send(
                text_data=json.dumps(
                    {"command": "profile_picture_updated", "status": "success"}
                )
            )

    async def teams_request(self):
        teams_dict = [
            {
                "id": str(team.id_uuid),
                "name": await sync_to_async(team.__str__)(),
                "logo": team.club.get_club_logo(),
                "get_absolute_url": str(team.get_absolute_url())
            }
            for team in self.teams
        ]

        await self.send(text_data=json.dumps({
            "command": "teams",
            "teams": teams_dict
        }))

    async def matches_request(self, command):
        wedstrijden_data = await self.get_matchs_data(
            ["upcoming", "active"] if command == "upcomming_matches" else ["finished"],
            "" if command == "upcomming_matches" else "-"
        )

        wedstrijden_dict = await transform_matchdata(wedstrijden_data)

        await self.send(text_data=json.dumps({
            "command": "matches",
            "wedstrijden": wedstrijden_dict
        }))

    async def get_matchs_data(self, status, order):
        matches = await sync_to_async(list)(Match.objects.filter(
            Q(home_team__team_data__in=self.team_data) |
            Q(away_team__team_data__in=self.team_data)
        ).distinct())

        matches_non_dub = list(dict.fromkeys(matches))

        matchs_data = await sync_to_async(list)(MatchData.objects.prefetch_related(
            "match_link", 
            "match_link__home_team", 
            "match_link__home_team__club", 
            "match_link__away_team", 
            "match_link__away_team__club"
        ).filter(
            match_link__in=matches_non_dub,
            status__in=status
        ).order_by(order + "match_link__start_time"))

        return matchs_data
