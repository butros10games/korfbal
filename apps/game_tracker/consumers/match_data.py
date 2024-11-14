from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.db.models import Q

from apps.game_tracker.models import (
    PlayerChange,
    Pause,
    PlayerGroup,
    GroupType,
    Shot,
    MatchPart,
    MatchData,
)
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import TeamData

from .common import get_time
from apps.common.utils import players_stats, general_stats

import json
import traceback


class MatchDataConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.match = None
        self.current_part = None

    async def connect(self):
        match_id = self.scope["url_route"]["kwargs"]["id"]
        self.match = await Match.objects.prefetch_related(
            "home_team", "away_team"
        ).aget(id_uuid=match_id)
        self.match_data = await MatchData.objects.aget(match_link=self.match)

        try:
            self.current_part = await MatchPart.objects.aget(
                match_data=self.match_data, active=True
            )
        except MatchPart.DoesNotExist:
            pass

        self.channel_names = [
            "detail_match_%s" % self.match.id_uuid,
            "tracker_match_%s" % self.match.id_uuid,
            "time_match_%s" % self.match.id_uuid,
        ]
        for channel_name in [self.channel_names[0], self.channel_names[2]]:
            await self.channel_layer.group_add(channel_name, self.channel_name)

        await self.accept()

    async def disconnect(self, close_code):
        for channel_name in self.channel_names:
            await self.channel_layer.group_discard(channel_name, self.channel_name)

    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data["command"]

            if command == "match_events":
                await self.get_events(user_id=json_data["user_id"])

            elif command == "get_time":
                await self.send(
                    text_data=await get_time(self.match_data, self.current_part)
                )

            elif command == "home_team" or command == "away_team":
                await self.team_request(command, json_data["user_id"])

            elif command == "savePlayerGroups":
                await self.save_player_groups_request(json_data["playerGroups"])

            elif command == "get_stats":
                data_type = json_data["data_type"]

                if data_type == "general":
                    await self.get_stats_general_request()

                elif data_type == "player_stats":
                    await self.get_stats_player_request()

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {"error": str(e), "traceback": traceback.format_exc()}
                )
            )

    async def team_request(self, command, user_id):
        team = self.match.home_team if command == "home_team" else self.match.away_team

        player_groups_array = await self.makePlayerGroupList(team)

        players_json = await self.makePlayerList(team)

        await self.send(
            text_data=json.dumps(
                {
                    "command": "playerGroups",
                    "playerGroups": player_groups_array,
                    "players": players_json,
                    "is_coach": await self.checkIfAcces(user_id, team),
                    "finished": True if self.match_data.status == "finished" else False,
                }
            )
        )

    async def save_player_groups_request(self, player_groups):
        for player_group in player_groups:
            group = await PlayerGroup.objects.aget(id_uuid=player_group["id"])
            await group.players.aclear()

            for player in player_group["players"]:
                if player == "NaN":
                    continue
                player_obj = await Player.objects.aget(id_uuid=player)
                await group.players.aadd(player_obj)

            await group.asave()

        await self.send(
            text_data=json.dumps({"command": "savePlayerGroups", "status": "success"})
        )

    async def get_stats_general_request(self):
        general_stats_json = await general_stats([self.match_data])

        await self.send(text_data=general_stats_json)

    async def get_stats_player_request(self):
        ## Get the player stats. shots for and against, goals for and against.
        players = await sync_to_async(list)(
            Player.objects.prefetch_related("user")
            .filter(
                Q(team_data_as_player__team=self.match.home_team)
                | Q(team_data_as_player__team=self.match.away_team)
            )
            .distinct()
        )

        player_stats = await players_stats(players, [self.match_data])

        await self.send(text_data=player_stats)

    async def get_events(self, event=None, user_id=None):
        try:
            events_dict = []

            # check if there is a part active or the match is finished
            if self.match_data.status != "upcomming":
                events = await self.get_all_events()

                for event in events:
                    if event.match_part is not None:
                        if isinstance(event, Shot):
                            events_dict.append(await self.event_shot(event))
                        elif isinstance(event, PlayerChange):
                            events_dict.append(await self.event_player_change(event))
                        elif isinstance(event, Pause):
                            events_dict.append(await self.event_pause(event))

            await self.send(
                text_data=json.dumps(
                    {
                        "command": "events",
                        "home_team_id": str(self.match.home_team.id_uuid),
                        "events": events_dict,
                        "access": (
                            await self.check_access(user_id, self.match)
                            if user_id
                            else False
                        ),
                        "status": self.match_data.status,
                    }
                )
            )

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {"error": str(e), "traceback": traceback.format_exc()}
                )
            )

    async def get_all_events(self):
        goals = await sync_to_async(list)(
            Shot.objects.prefetch_related(
                "player__user", "shot_type", "match_part", "team"
            )
            .filter(match_data=self.match_data, scored=True)
            .order_by("time")
        )
        player_change = await sync_to_async(list)(
            PlayerChange.objects.prefetch_related(
                "player_in",
                "player_in__user",
                "player_out",
                "player_out__user",
                "player_group",
                "player_group__team",
                "match_part",
            )
            .filter(player_group__match_data=self.match_data)
            .order_by("time")
        )
        time_outs = await sync_to_async(list)(
            Pause.objects.prefetch_related("match_part")
            .filter(match_data=self.match_data)
            .order_by("start_time")
        )

        # add all the events to a list and order them on time
        events = []
        events.extend(goals)
        events.extend(player_change)
        events.extend(time_outs)
        events.sort(key=lambda x: getattr(x, "time", getattr(x, "start_time", None)))

        return events

    async def event_shot(self, event):
        # calculate the time of the pauses before the event happend. By requesting the pauses that are before the event and summing the length of the pauses
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event.time,
                start_time__gte=event.match_part.start_time,
            )
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        time_in_minutes = round(
            (
                (event.time - event.match_part.start_time).total_seconds()
                + (
                    int(event.match_part.part_number - 1)
                    * int(self.match_data.part_lenght)
                )
                - pause_time
            )
            / 60
        )

        left_over = time_in_minutes - (
            (event.match_part.part_number * self.match_data.part_lenght) / 60
        )
        if left_over > 0:
            time_in_minutes = (
                str(time_in_minutes - left_over).split(".")[0]
                + "+"
                + str(left_over).split(".")[0]
            )

        return {
            "type": "goal",
            "time": time_in_minutes,
            "player": event.player.user.username,
            "goal_type": event.shot_type.name,
            "for_team": event.for_team,
            "team_id": str(event.team.id_uuid),
        }

    async def event_player_change(self, event):
        # calculate the time of the pauses before the event happend. By requesting the pauses that are before the event and summing the length of the pauses
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event.time,
                start_time__gte=event.match_part.start_time,
            )
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        time_in_minutes = round(
            (
                (event.time - event.match_part.start_time).total_seconds()
                + ((event.match_part.part_number - 1) * self.match_data.part_lenght)
                - pause_time
            )
            / 60
        )

        left_over = time_in_minutes - (
            (event.match_part.part_number * self.match_data.part_lenght) / 60
        )
        if left_over > 0:
            time_in_minutes = (
                str(time_in_minutes - left_over).split(".")[0]
                + "+"
                + str(left_over).split(".")[0]
            )

        return {
            "type": "wissel",
            "time": time_in_minutes,
            "player_in": event.player_in.user.username,
            "player_out": event.player_out.user.username,
            "player_group": str(event.player_group.id_uuid),
        }

    async def event_pause(self, event):
        # calculate the time of the pauses before the event happend. By requesting the pauses that are before the event and summing the length of the pauses
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event.start_time,
                start_time__gte=event.match_part.start_time,
            )
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        # calculate the time in minutes sinds the real_start_time of the match and the start_time of the pause
        time_in_minutes = round(
            (
                (event.start_time - event.match_part.start_time).total_seconds()
                + (
                    int(event.match_part.part_number - 1)
                    * int(self.match_data.part_lenght)
                )
                - pause_time
            )
            / 60
        )

        left_over = time_in_minutes - (
            (event.match_part.part_number * self.match_data.part_lenght) / 60
        )
        if left_over > 0:
            time_in_minutes = (
                str(time_in_minutes - left_over).split(".")[0]
                + "+"
                + str(left_over).split(".")[0]
            )

        return {
            "type": "pauze",
            "time": time_in_minutes,
            "length": event.length().total_seconds(),
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
        }

    async def check_access(self, user_id, match):
        player = await Player.objects.aget(user=user_id)

        # Combine queries for players and coaches for both home and away teams
        team_data = await sync_to_async(list)(
            TeamData.objects.prefetch_related("players", "coach")
            .filter(Q(team=match.home_team) | Q(team=match.away_team))
            .values_list("players", "coach")
        )

        # Flatten the list of tuples and remove None values
        players_list = [
            item for sublist in team_data for item in sublist if item is not None
        ]

        access = player.id_uuid in players_list
        return access

    async def makePlayerGroupList(self, team):
        try:
            player_groups = await sync_to_async(list)(
                PlayerGroup.objects.prefetch_related(
                    "players", "players__user", "starting_type", "current_type"
                )
                .filter(match_data=self.match_data, team=team)
                .order_by("starting_type")
            )

            # When there is no connected player group create the player groups
            if player_groups == []:
                group_types = await sync_to_async(list)(GroupType.objects.all())

                for group_type in group_types:
                    await PlayerGroup.objects.acreate(
                        match_data=self.match_data,
                        team=team,
                        starting_type=group_type,
                        current_type=group_type,
                    )

                player_groups = await sync_to_async(list)(
                    PlayerGroup.objects.prefetch_related(
                        "players", "players__user", "starting_type", "current_type"
                    )
                    .filter(match_data=self.match_data, team=team)
                    .order_by("starting_type")
                )

            # make it a json parsable string
            player_groups_array = [
                {
                    "id": str(player_group.id_uuid),
                    "players": [
                        {
                            "id": str(player.id_uuid),
                            "name": player.user.username,
                        }
                        for player in player_group.players.all()
                    ],
                    "starting_type": player_group.starting_type.name,
                    "current_type": player_group.current_type.name,
                }
                for player_group in player_groups
            ]

            return player_groups_array

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                        "player_groups": player_groups,
                    }
                )
            )

    async def makePlayerList(self, team):
        # get the season of the match
        season = await self.season_request()

        players_json = []
        players = await sync_to_async(list)(
            TeamData.objects.prefetch_related("players")
            .filter(team=team, season=season)
            .values_list("players", flat=True)
        )

        for player in players:
            try:
                player_json = await Player.objects.prefetch_related("user").aget(
                    id_uuid=player
                )

                players_json.append(
                    {
                        "id": str(player_json.id_uuid),
                        "name": player_json.user.username,
                        "profile_picture": player_json.get_profile_picture(),
                        "get_absolute_url": str(player_json.get_absolute_url()),
                    }
                )
            except Player.DoesNotExist:
                pass

        # remove duplicates
        players_json = [dict(t) for t in {tuple(d.items()) for d in players_json}]

        return players_json

    async def checkIfAcces(self, user_id, team):
        player = await Player.objects.aget(user=user_id)

        players = await sync_to_async(list)(
            TeamData.objects.prefetch_related("players")
            .filter(team=team)
            .values_list("players", flat=True)
        )
        coaches = await sync_to_async(list)(
            TeamData.objects.prefetch_related("coach")
            .filter(team=team)
            .values_list("coach", flat=True)
        )

        players_list = []

        players_list.extend(players)
        players_list.extend(coaches)

        access = False
        if player.id_uuid in players_list:
            access = True

        return access

    async def send_data(self, event):
        data = event["data"]
        await self.send(text_data=json.dumps(data))

    async def season_request(self):
        try:
            return await Season.objects.aget(
                start_date__lte=self.match.start_time,
                end_date__gte=self.match.start_time,
            )
        except Season.DoesNotExist:
            return (
                await Season.objects.filter(end_date__lte=self.match.start_time)
                .order_by("-end_date")
                .afirst()
            )
