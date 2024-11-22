import json
import traceback
from datetime import datetime

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.game_tracker.models import (
    GoalType,
    GroupType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    Shot,
)
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData
from django.db.models import Case, When
from django.utils.timezone import make_aware

from .common import get_time


class MatchTrackerConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.match = None
        self.current_part = None
        self.is_paused = False
        self.match_is_paused_message = "match is paused"
        self.player_group_class = PlayerGroupClass(self.season_request)

    async def connect(self):
        match_id = self.scope["url_route"]["kwargs"]["id"]
        self.match = await Match.objects.prefetch_related(
            "home_team", "away_team"
        ).aget(id_uuid=match_id)
        self.match_data = await MatchData.objects.aget(match_link=self.match)
        self.team = await Team.objects.aget(
            id_uuid=self.scope["url_route"]["kwargs"]["team_id"]
        )
        try:
            self.current_part = await MatchPart.objects.aget(
                match_data=self.match_data, active=True
            )
        except MatchPart.DoesNotExist:
            pass

        # Check if an active pause exists for the given match_data
        is_pause_active = await sync_to_async(
            Pause.objects.filter(match_data=self.match_data, active=True).exists
        )()

        # Set the pause status based on the existence of an active pause or the match
        # status
        self.is_paused = is_pause_active or self.match_data.status != "active"

        if self.team == self.match.home_team:
            self.other_team = self.match.away_team
        else:
            self.other_team = self.match.home_team

        self.channel_names = [
            "detail_match_%s" % self.match.id_uuid,
            "tracker_match_%s" % self.match.id_uuid,
            "time_match_%s" % self.match.id_uuid,
        ]
        for channel_name in [self.channel_names[1], self.channel_names[2]]:
            await self.channel_layer.group_add(channel_name, self.channel_name)

        self.player_group_class.team = self.team
        self.player_group_class.match_data = self.match_data

        await self.accept()

        if self.match_data.status == "finished":
            await self.send(
                text_data=json.dumps(
                    {"command": "match_end", "match_id": str(self.match.id_uuid)}
                )
            )

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.channel_names[0], self.channel_name)
        await self.channel_layer.group_discard(self.channel_names[1], self.channel_name)
        await self.channel_layer.group_discard(self.channel_names[2], self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        try:
            json_data = json.loads(text_data)
            command = json_data["command"]

            if command == "playerGroups":
                await self.send(
                    text_data=await self.player_group_class.player_group_request()
                )

            elif command == "savePlayerGroups":
                await self.save_player_groups(json_data["playerGroups"])

            elif command == "shot_reg":
                await self.shot_reg(
                    json_data["player_id"], json_data["time"], json_data["for_team"]
                )

            elif command == "get_goal_types":
                await self.get_goal_types()

            elif command == "goal_reg":
                await self.goal_reg(
                    json_data["player_id"],
                    json_data["goal_type"],
                    json_data["for_team"],
                    json_data["time"],
                )

            elif command == "start/pause":
                await self.start_pause()

            elif command == "part_end":
                await self.part_end()

            elif command == "get_time":
                await self.send(
                    text_data=await get_time(self.match_data, self.current_part)
                )

            elif command == "last_event":
                await self.send_last_event()

            elif command == "get_non_active_players":
                await self.get_non_active_players()

            elif command == "wissel_reg":
                await self.wissel_reg(
                    json_data["new_player_id"],
                    json_data["old_player_id"],
                    json_data["time"],
                )

            elif command == "remove_last_event":
                await self.removed_last_event()

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {"error": str(e), "traceback": traceback.format_exc()}
                )
            )

    async def save_player_groups(self, player_groups):
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

    async def shot_reg(self, player_id, time, for_team):
        # check if the match is paused and if it is paused decline the request except
        # for the start/stop command
        if self.is_paused:
            await self.send(
                text_data=json.dumps({"error": self.match_is_paused_message})
            )
            return

        await Shot.objects.acreate(
            player=await Player.objects.aget(id_uuid=player_id),
            match_data=self.match_data,
            match_part=self.current_part,
            time=time,
            for_team=for_team,
        )

        await self.channel_layer.group_send(
            self.channel_names[1],
            {
                "type": "send_data",
                "data": {
                    "command": "player_shot_change",
                    "player_id": player_id,
                    "shots_for": await Shot.objects.filter(
                        player__id_uuid=player_id,
                        match_data=self.match_data,
                        for_team=True,
                    ).acount(),
                    "shots_against": await Shot.objects.filter(
                        player__id_uuid=player_id,
                        match_data=self.match_data,
                        for_team=False,
                    ).acount(),
                },
            },
        )

        await self.send_last_event()

    async def get_goal_types(self):
        goal_type_list = await sync_to_async(list)(GoalType.objects.all())

        goal_type_list = [
            {"id": str(goal_type.id_uuid), "name": goal_type.name}
            for goal_type in goal_type_list
        ]

        await self.send(
            text_data=json.dumps(
                {"command": "goal_types", "goal_types": goal_type_list}
            )
        )

    async def goal_reg(self, player_id, goal_type, for_team, time):
        # check if the match is paused and if it is paused decline the request except
        # for the start/stop command
        if self.is_paused:
            await self.send(
                text_data=json.dumps({"error": self.match_is_paused_message})
            )
            return

        if for_team:
            team = self.team
        else:
            team = self.other_team

        player = await Player.objects.prefetch_related("user").aget(id_uuid=player_id)
        goal_type = await GoalType.objects.aget(id_uuid=goal_type)

        await Shot.objects.acreate(
            player=await Player.objects.aget(id_uuid=player_id),
            match_data=self.match_data,
            match_part=self.current_part,
            time=time,
            shot_type=goal_type,
            for_team=for_team,
            team=team,
            scored=True,
        )

        for channel_name in [self.channel_names[1], self.channel_names[0]]:
            await self.channel_layer.group_send(
                channel_name,
                {
                    "type": "send_data",
                    "data": {
                        "command": "player_shot_change",
                        "player_id": player_id,
                        "shots_for": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            for_team=True,
                        ).acount(),
                        "shots_against": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            for_team=False,
                        ).acount(),
                    },
                },
            )

        for channel_name in [self.channel_names[1], self.channel_names[0]]:
            await self.channel_layer.group_send(
                channel_name,
                {
                    "type": "send_data",
                    "data": {
                        "command": "team_goal_change",
                        "player_name": player.user.username,
                        "goal_type": goal_type.name,
                        "goals_for": await Shot.objects.filter(
                            match_data=self.match_data,
                            team=self.team,
                            scored=True,
                        ).acount(),
                        "goals_against": await Shot.objects.filter(
                            match_data=self.match_data,
                            team=self.other_team,
                            scored=True,
                        ).acount(),
                    },
                },
            )

        number_of_shots = await Shot.objects.filter(
            match_data=self.match_data, scored=True
        ).acount()
        if number_of_shots % 2 == 0:
            await self.player_group_class.swap_player_group_types(self.team)
            await self.player_group_class.swap_player_group_types(self.other_team)

            await self.send(
                text_data=await self.player_group_class.player_group_request()
            )

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0], {"type": "get_events"}
        )

    async def start_pause(self):
        try:
            part = await MatchPart.objects.aget(match_data=self.match_data, active=True)
        except MatchPart.DoesNotExist:
            part = await MatchPart.objects.acreate(
                match_data=self.match_data,
                active=True,
                start_time=datetime.now(),
                part_number=self.match_data.current_part,
            )

            # reload part from database
            part = await MatchPart.objects.aget(match_data=self.match_data, active=True)

            self.current_part = part

            self.is_paused = False

            if self.match_data.current_part == 1:
                self.match_data.status = "active"
                await self.match_data.asave()

                await self.send(
                    text_data=await self.player_group_class.player_group_request()
                )

            for channel_name in [self.channel_names[2]]:
                await self.channel_layer.group_send(
                    channel_name,
                    {
                        "type": "send_data",
                        "data": {
                            "command": "timer_data",
                            "type": "start",
                            "time": part.start_time.isoformat(),
                            "length": self.match_data.part_lenght,
                        },
                    },
                )
        else:
            try:
                pause = await Pause.objects.aget(
                    match_data=self.match_data,
                    active=True,
                    match_part=self.current_part,
                )
            except Pause.DoesNotExist:
                pause = await Pause.objects.acreate(
                    match_data=self.match_data,
                    active=True,
                    start_time=datetime.now(),
                    match_part=self.current_part,
                )

                self.is_paused = True
                pause_message = {"command": "pause", "pause": True}
            else:
                naive_datetime = datetime.now()
                aware_datetime = make_aware(naive_datetime)
                pause.active = False
                pause.end_time = aware_datetime
                await pause.asave()

                self.is_paused = False

                pauses = await sync_to_async(list)(
                    Pause.objects.filter(
                        match_data=self.match_data,
                        active=False,
                        match_part=self.current_part,
                    )
                )
                pause_time = 0
                for pause in pauses:
                    pause_time += pause.length().total_seconds()

                pause_message = {
                    "command": "pause",
                    "pause": False,
                    "pause_time": pause_time,
                }

            for channel_name in [self.channel_names[2]]:
                await self.channel_layer.group_send(
                    channel_name, {"type": "send_data", "data": pause_message}
                )

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0], {"type": "get_events"}
        )

    async def part_end(self):
        try:
            pause = await Pause.objects.aget(
                match_data=self.match_data,
                active=True,
                match_part=self.current_part,
            )

            pause.active = False
            pause.end_time = make_aware(datetime.now())
            await pause.asave()

        except Pause.DoesNotExist:
            pass

        if self.match_data.current_part < self.match_data.parts:
            self.match_data.current_part += 1
            await self.match_data.asave()

            try:
                match_part = await MatchPart.objects.aget(
                    match_data=self.match_data, active=True
                )
                match_part.active = False
                match_part.end_time = datetime.now()
                await match_part.asave()

            except MatchPart.DoesNotExist:
                pass

            for channel_name in [self.channel_names[2]]:
                await self.channel_layer.group_send(
                    channel_name,
                    {
                        "type": "send_data",
                        "data": {
                            "command": "part_end",
                            "part": self.match_data.current_part,
                            "part_length": self.match_data.part_lenght,
                        },
                    },
                )
        else:
            self.match_data.status = "finished"
            await self.match_data.asave()

            match_part = await MatchPart.objects.aget(
                match_data=self.match_data, active=True
            )
            match_part.active = False
            match_part.end_time = make_aware(datetime.now())
            await match_part.asave()

            for channel_name in [self.channel_names[2]]:
                await self.channel_layer.group_send(
                    channel_name,
                    {
                        "type": "send_data",
                        "data": {
                            "command": "match_end",
                            "match_id": str(self.match.id_uuid),
                        },
                    },
                )

    async def get_non_active_players(self):
        # Get the players that are currently in the reserve player group
        reserve_group = await PlayerGroup.objects.prefetch_related(
            "players", "players__user"
        ).aget(
            match_data=self.match_data, team=self.team, starting_type__name="Reserve"
        )

        players_json = []
        for player in reserve_group.players.all():
            try:
                players_json.append(
                    {"id": str(player.id_uuid), "name": player.user.username}
                )
            except Player.DoesNotExist:
                pass

        await self.send(
            text_data=json.dumps(
                {"command": "non_active_players", "players": players_json}
            )
        )

    async def wissel_reg(self, new_player_id, old_player_id, time):
        # check if the match is paused and if it is paused decline the request except
        # for the start/stop command
        if self.is_paused:
            await self.send(
                text_data=json.dumps({"error": self.match_is_paused_message})
            )
            return

        player_in = await Player.objects.prefetch_related("user").aget(
            id_uuid=new_player_id
        )
        player_out = await Player.objects.prefetch_related("user").aget(
            id_uuid=old_player_id
        )

        player_reserve_group = await PlayerGroup.objects.aget(
            team=self.team,
            match_data=self.match_data,
            starting_type__name="Reserve",
        )
        player_group = await PlayerGroup.objects.aget(
            team=self.team, match_data=self.match_data, players__in=[player_out]
        )

        await player_group.players.aremove(player_out)
        await player_reserve_group.players.aadd(player_out)

        await player_reserve_group.players.aremove(player_in)
        await player_group.players.aadd(player_in)

        await PlayerChange.objects.acreate(
            player_in=player_in,
            player_out=player_out,
            player_group=player_group,
            match_data=self.match_data,
            match_part=self.current_part,
            time=time,
        )

        # get the shot count for the new player
        shots_for = await Shot.objects.filter(
            player=player_in, match_data=self.match_data, for_team=True
        ).acount()
        shots_against = await Shot.objects.filter(
            player=player_in, match_data=self.match_data, for_team=False
        ).acount()

        for channel_name in [self.channel_names[1]]:
            await self.channel_layer.group_send(
                channel_name,
                {
                    "type": "send_data",
                    "data": {
                        "command": "player_change",
                        "player_in": player_in.user.username,
                        "player_in_id": str(player_in.id_uuid),
                        "player_in_shots_for": shots_for,
                        "player_in_shots_against": shots_against,
                        "player_out": player_out.user.username,
                        "player_out_id": str(player_out.id_uuid),
                        "player_group": str(player_group.id_uuid),
                    },
                },
            )

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0], {"type": "get_events"}
        )

    async def removed_last_event(self):
        event = await self.get_all_events()

        if isinstance(event, Shot):
            player_id = str(event.player.id_uuid)

            await event.adelete()

            # send player shot update message
            await self.channel_layer.group_send(
                self.channel_names[1],
                {
                    "type": "send_data",
                    "data": {
                        "command": "player_shot_change",
                        "player_id": player_id,
                        "shots_for": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            for_team=True,
                        ).acount(),
                        "shots_against": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            for_team=False,
                        ).acount(),
                    },
                },
            )

            # check if the shot was a goal and if it was a goal check if it was a switch
            # goal and if it was a switch goal swap the player group types back
            if not event.scored:
                return

            for channel_name in [self.channel_names[1], self.channel_names[0]]:
                await self.channel_layer.group_send(
                    channel_name,
                    {
                        "type": "send_data",
                        "data": {
                            "command": "team_goal_change",
                            "player_name": event.player.user.username,
                            "goal_type": event.shot_type.name,
                            "goals_for": await Shot.objects.filter(
                                match_data=self.match_data,
                                team=self.team,
                                scored=True,
                            ).acount(),
                            "goals_against": await Shot.objects.filter(
                                match_data=self.match_data,
                                team=self.other_team,
                                scored=True,
                            ).acount(),
                        },
                    },
                )

            number_of_shots = await Shot.objects.filter(
                match_data=self.match_data, scored=True
            ).acount()
            if (number_of_shots) % 2 == 1:
                await self.player_group_class.swap_player_group_types(self.team)
                await self.player_group_class.swap_player_group_types(self.other_team)

                await self.send(
                    text_data=await self.player_group_class.player_group_request()
                )

        elif isinstance(event, PlayerChange):
            # get and delete the last player change event
            player_change = await PlayerChange.objects.prefetch_related(
                "player_group", "player_in", "player_out"
            ).aget(match_part=event.match_part, time=event.time)
            player_group = await PlayerGroup.objects.aget(
                id_uuid=player_change.player_group.id_uuid
            )
            player_reserve_group = await PlayerGroup.objects.aget(
                team=self.team,
                match_data=self.match_data,
                starting_type__name="Reserve",
            )

            await player_group.players.aremove(player_change.player_in)
            await player_reserve_group.players.aadd(player_change.player_in)

            await player_group.players.aadd(player_change.player_out)
            await player_reserve_group.players.aremove(player_change.player_out)

            await player_group.asave()
            await player_reserve_group.asave()

            await player_change.adelete()

            # send player group update message
            await self.send(
                text_data=await self.player_group_class.player_group_request()
            )

        elif isinstance(event, Pause):
            # get and delete the last pause event
            pause = await Pause.objects.aget(
                active=event.active,
                match_part=event.match_part,
                start_time=event.start_time,
            )

            if event.active:
                await pause.adelete()
            else:
                pause.active = True
                pause.end_time = None
                await pause.asave()

            time_data = await get_time(self.match_data, self.current_part)

            await self.channel_layer.group_send(
                self.channel_names[2],
                {"type": "send_data", "data": json.loads(time_data)},
            )

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0], {"type": "get_events"}
        )

    async def get_all_events(self):
        # Fetch each type of event separately
        shots = await sync_to_async(list)(
            Shot.objects.prefetch_related(
                "player__user",
                "match_part",
                "shot_type",
                "match_data",
            )
            .filter(match_data=self.match_data)
            .order_by("time")
        )
        player_changes = await sync_to_async(list)(
            PlayerChange.objects.prefetch_related(
                "player_in",
                "player_in__user",
                "player_out",
                "player_out__user",
                "player_group",
                "match_part",
                "match_data",
            )
            .filter(player_group__match_data=self.match_data)
            .order_by("time")
        )
        time_outs = await sync_to_async(list)(
            Pause.objects.prefetch_related(
                "match_part",
                "match_part__match_data",
                "match_data__match_link",
            )
            .filter(match_data=self.match_data)
            .order_by("start_time")
        )

        # Combine all events and sort them
        events = sorted(
            shots + player_changes + time_outs,
            key=lambda x: getattr(x, "time", getattr(x, "start_time", None)),
        )

        # check if there are events
        if events == []:
            await self.send(
                text_data=json.dumps(
                    {
                        "command": "last_event",
                        "last_event": {
                            "type": "no_event",
                        },
                    }
                )
            )
            return None

        # Get last event
        return events[-1]

    async def send_last_event(self):
        last_event = await self.get_all_events()

        if last_event is None:
            return

        if isinstance(last_event, Shot):
            time_in_minutes = await self.time_calc(last_event)

            data_add = {"type": "shot"}
            if last_event.scored:
                goals_for = await Shot.objects.filter(
                    match_data=self.match_data, for_team=True, scored=True
                ).acount()
                goals_against = await Shot.objects.filter(
                    match_data=self.match_data, for_team=False, scored=True
                ).acount()

                data_add = {
                    "type": "goal",
                    "goals_for": goals_for,
                    "goals_against": goals_against,
                }

            events_dict = {
                "time": time_in_minutes,
                "player": last_event.player.user.username,
                "shot_type": (
                    last_event.shot_type.name if last_event.shot_type else None,
                ),
                "for_team": last_event.for_team,
                **data_add,
            }
        elif isinstance(last_event, PlayerChange):
            player_in_username = last_event.player_in.user.username
            player_out_username = last_event.player_out.user.username

            time_in_minutes = await self.time_calc(last_event)

            events_dict = {
                "type": "wissel",
                "time": time_in_minutes,
                "player_in": player_in_username,
                "player_out": player_out_username,
                "player_group": str(last_event.player_group.id_uuid),
            }
        elif isinstance(last_event, Pause):
            time_in_minutes = await self.time_calc(last_event)

            events_dict = {
                "type": "pause",
                "time": time_in_minutes,
                "length": last_event.length().total_seconds(),
                "start_time": (
                    last_event.start_time.isoformat() if last_event.start_time else None
                ),
                "end_time": (
                    last_event.end_time.isoformat() if last_event.end_time else None
                ),
            }

        await self.send(
            text_data=json.dumps({"command": "last_event", "last_event": events_dict})
        )

    async def time_calc(self, event):
        # Determine the event time attribute, either "time" or "start_time"
        event_time = getattr(event, "time", getattr(event, "start_time", None))

        if not event_time:
            raise ValueError("Event must have either `time` or `start_time` attribute")

        # Calculate the time of the pauses before the event happened by summing the
        # length of the pauses
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event_time,
                start_time__gte=event.match_part.start_time,
            )
        )
        pause_time = sum([pause.length().total_seconds() for pause in pauses])

        # Calculate the time in minutes since the real_start_time of the match and the
        # event time
        time_in_minutes = round(
            (
                (event_time - event.match_part.start_time).total_seconds()
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

        return time_in_minutes

    # is for the data that needs be send from other websocket connections
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
            return await sync_to_async(
                Season.objects.filter(end_date__lte=self.match.start_time)
                .order_by("-end_date")
                .first
            )()


class PlayerGroupClass:
    def __init__(self, season_request):
        self.team = None
        self.match_data = None
        self._season_request = season_request

    async def player_group_request(self):
        player_groups_array = await self._make_player_group_list()

        return json.dumps(
            {
                "command": "playerGroups",
                "playerGroups": player_groups_array,
                "players": await self._make_full_player_list(),
                "match_active": self.match_data.status == "active",
            }
        )

    async def get_player_groups(self):
        return await sync_to_async(list)(
            PlayerGroup.objects.prefetch_related(
                "players", "players__user", "starting_type", "current_type"
            )
            .filter(match_data=self.match_data, team=self.team)
            .exclude(starting_type__name="Reserve")
            .order_by(
                Case(When(current_type__name="Aanval", then=0), default=1),
                "starting_type",
            )
        )

    async def swap_player_group_types(self, team):
        group_type_a = await GroupType.objects.aget(name="Aanval")
        group_type_v = await GroupType.objects.aget(name="Verdediging")

        player_group_a = await PlayerGroup.objects.aget(
            match_data=self.match_data, team=team, current_type=group_type_a
        )
        player_group_v = await PlayerGroup.objects.aget(
            match_data=self.match_data, team=team, current_type=group_type_v
        )

        player_group_a.current_type = group_type_v
        player_group_v.current_type = group_type_a

        await player_group_a.asave()
        await player_group_v.asave()

    async def _make_player_group_list(self):
        player_groups = await self.get_player_groups()
        if not player_groups:
            await self._create_player_groups()
            player_groups = await self.get_player_groups()
        return await self._make_player_group_json(player_groups)

    async def _create_player_groups(self):
        group_types = await sync_to_async(list)(GroupType.objects.all().order_by("id"))
        for group_type in group_types:
            await PlayerGroup.objects.acreate(
                match_data=self.match_data,
                team=self.team,
                starting_type=group_type,
                current_type=group_type,
            )

    async def _make_player_group_json(self, player_groups):
        async def _process_player(player):
            return {
                "id": str(player.id_uuid),
                "name": player.user.username,
                "shots_for": await Shot.objects.filter(
                    player=player, match_data=self.match_data, for_team=True
                ).acount(),
                "shots_against": await Shot.objects.filter(
                    player=player, match_data=self.match_data, for_team=False
                ).acount(),
            }

        async def _process_player_group(player_group):
            return {
                "id": str(player_group.id_uuid),
                "players": [
                    await _process_player(player)
                    for player in player_group.players.all()
                ],
                "starting_type": player_group.starting_type.name,
                "current_type": player_group.current_type.name,
            }

        return [
            await _process_player_group(player_group) for player_group in player_groups
        ]

    async def _make_full_player_list(self):
        season = await self._season_request()
        players_json = []
        players = await sync_to_async(list)(
            TeamData.objects.prefetch_related("players")
            .filter(team=self.team, season=season)
            .values_list("players", flat=True)
        )

        for player in players:
            player_json = await self._get_player_json(player)
            if player_json:
                players_json.append(player_json)

        return self._remove_duplicates(players_json)

    async def _get_player_json(self, player):
        try:
            player_obj = await Player.objects.prefetch_related("user").aget(
                id_uuid=player
            )
            return {
                "id": str(player_obj.id_uuid),
                "name": player_obj.user.username,
                "profile_picture": player_obj.get_profile_picture(),
                "get_absolute_url": str(player_obj.get_absolute_url()),
            }
        except Player.DoesNotExist:
            return None

    def _remove_duplicates(self, players_json):
        return [dict(t) for t in {tuple(d.items()) for d in players_json}]
