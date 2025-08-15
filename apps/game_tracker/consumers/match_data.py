"""Module contains the MatchDataConsumer class. This class is used to handle the
websocket connection for the match data.
"""

import contextlib
from datetime import UTC, datetime
import json
import traceback
from uuid import UUID

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q

from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    Shot,
    Timeout,
)
from apps.kwt_common.utils import general_stats, get_time, players_stats
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


class MatchDataConsumer(AsyncWebsocketConsumer):
    """Class is used to handle the websocket connection for the match data."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Initialize the MatchDataConsumer."""
        super().__init__(*args, **kwargs)
        self.match = None
        self.current_part = None

    async def connect(self) -> None:
        """Connect to the websocket."""
        match_id = self.scope["url_route"]["kwargs"]["id"]
        self.match = await Match.objects.prefetch_related(
            "home_team",
            "away_team",
        ).aget(id_uuid=match_id)
        self.match_data = await MatchData.objects.aget(match_link=self.match)

        with contextlib.suppress(MatchPart.DoesNotExist):
            self.current_part = await MatchPart.objects.aget(
                match_data=self.match_data,
                active=True,
            )

        self.channel_names = [
            f"detail_match_{self.match.id_uuid}",
            f"tracker_match_{self.match.id_uuid}",
            f"time_match_{self.match.id_uuid}",
        ]
        for channel_name in [self.channel_names[0], self.channel_names[2]]:
            await self.channel_layer.group_add(channel_name, self.channel_name)

        await self.accept()

    async def disconnect(self, code: int) -> None:
        """Disconnect from the websocket."""
        for channel_name in self.channel_names:
            await self.channel_layer.group_discard(channel_name, self.channel_name)

    async def receive(
        self,
        text_data: str | None = None,
        bytes_data: bytes | None = None,
    ) -> None:
        """Receive data from the websocket.

        Args:
            text_data: The data received from the websocket.
            bytes_data: The bytes data received from the websocket.

        """
        if bytes_data:
            text_data = bytes_data.decode("utf-8")
        if text_data is None:
            await self.send(text_data=json.dumps({"error": "No data received"}))
            return

        json_data = json.loads(text_data)
        command = json_data["command"]

        if command == "match_events":
            await self.get_events(user_id=json_data["user_id"])

        elif command == "get_time":
            await self.send(
                text_data=await get_time(self.match_data, self.current_part),
            )

        elif command in {"home_team", "away_team"}:
            await self.team_request(command, json_data["user_id"])

        elif command == "savePlayerGroups":
            await self.save_player_groups_request(json_data["playerGroups"])

        elif command == "get_stats":
            data_type = json_data["data_type"]

            if data_type == "general":
                await self.get_stats_general_request()

            elif data_type == "player_stats":
                await self.get_stats_player_request()

    async def team_request(self, command: str, user_id: UUID | None) -> None:
        """Get the team data for the home or away team.

        Args:
            command: The command to get the home or away team.
            user_id: The id of the user.

        """
        if self.match is None:
            return

        team = self.match.home_team if command == "home_team" else self.match.away_team

        await self.send(
            text_data=json.dumps(
                {
                    "command": "playerGroups",
                    "match_id": str(self.match.id_uuid),
                    "team_id": str(team.id_uuid),
                    "group_id_to_type_id": {
                        str(group.id_uuid): str(group.starting_type.id_uuid)
                        for group in await sync_to_async(list)(
                            PlayerGroup.objects.prefetch_related(
                                "starting_type",
                            ).filter(team=team, match_data=self.match_data),
                        )
                    },
                    "type_id_to_group_id": {
                        str(group.starting_type.id_uuid): str(group.id_uuid)
                        for group in await sync_to_async(list)(
                            PlayerGroup.objects.prefetch_related(
                                "starting_type",
                            ).filter(team=team, match_data=self.match_data),
                        )
                    },
                    "is_coach": await self.check_if_access(user_id, team),
                    "finished": self.match_data.status == "finished",
                },
            ),
        )

    async def save_player_groups_request(self, player_groups: list[dict]) -> None:
        """Save the player groups.

        Args:
            player_groups: The player groups to save.

        """
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
            text_data=json.dumps({"command": "savePlayerGroups", "status": "success"}),
        )

    async def get_stats_general_request(self) -> None:
        """Get the general stats for the match."""
        general_stats_json = await general_stats([self.match_data])

        await self.send(text_data=general_stats_json)

    async def get_stats_player_request(self) -> None:
        """Get the player stats for the match."""
        if self.match is None:
            return

        players = await sync_to_async(list)(
            Player.objects.prefetch_related("user")
            .filter(
                Q(team_data_as_player__team=self.match.home_team)
                | Q(team_data_as_player__team=self.match.away_team),
            )
            .distinct(),
        )

        player_stats = await players_stats(players, [self.match_data])

        await self.send(text_data=player_stats)

    async def get_events(
        self,
        event: str | None = None,
        user_id: UUID | None = None,
    ) -> None:
        """Get the events for the match.

        Args:
            event: The event to get.
            user_id: The id of the user.

        """
        try:
            events_dict = []

            # check if there is a part active or the match is finished
            if self.match_data.status != "upcoming":
                events = await self.get_all_events()

                for event_d in events:
                    if event_d.match_part is not None:
                        if isinstance(event_d, Shot):
                            events_dict.append(await self.event_shot(event_d))
                        elif isinstance(event_d, PlayerChange):
                            events_dict.append(await self.event_player_change(event_d))
                        elif isinstance(event_d, Pause):
                            events_dict.append(await self.event_pause(event_d))

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
                    },
                ),
            )

        except Exception as e:
            await self.send(
                text_data=json.dumps(
                    {"error": str(e), "traceback": traceback.format_exc()},
                ),
            )

    async def get_all_events(self) -> list:
        """Get all the events for the match.

        Returns:
            A list of all the events for the match.

        """
        goals = await sync_to_async(list)(
            Shot.objects.prefetch_related(
                "player__user",
                "shot_type",
                "match_part",
                "team",
            )
            .filter(match_data=self.match_data, scored=True)
            .order_by("time"),
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
            .order_by("time"),
        )
        time_outs = await sync_to_async(list)(
            Pause.objects.prefetch_related("match_part")
            .filter(match_data=self.match_data)
            .order_by("start_time"),
        )

        # add all the events to a list and order them on time
        events = []
        events.extend(goals)
        events.extend(player_change)
        events.extend(time_outs)

        def event_time_key(x: object) -> datetime:
            value = getattr(x, "time", None)
            if value is not None:
                return value
            value = getattr(x, "start_time", None)
            if value is not None:
                return value
            return datetime.min.replace(tzinfo=UTC)

        events.sort(key=event_time_key)

        return events

    async def event_shot(self, event: Shot) -> dict:
        """Get the event for a shot.

        Args:
            event: The event to get.

        Returns:
            The event for a shot.

        """
        # calculate the time of the pauses before the event happened. By requesting the
        # pauses that are before the event and summing the length of the pauses
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event.time,
                start_time__gte=event.match_part.start_time,
            ),
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        time_in_minutes = round(
            (
                (event.time - event.match_part.start_time).total_seconds()
                + (
                    int(event.match_part.part_number - 1)
                    * int(self.match_data.part_length)
                )
                - pause_time
            )
            / 60,
        )

        left_over = time_in_minutes - (
            (event.match_part.part_number * self.match_data.part_length) / 60
        )
        if left_over > 0:
            time_in_minutes = (
                str(time_in_minutes - left_over).split(".")[0]
                + "+"
                + str(left_over).split(".")[0]
            )

        return {
            "type": "goal",
            "name": "Gescoord",
            "time": time_in_minutes,
            "player": event.player.user.username,
            "goal_type": event.shot_type.name,
            "for_team": event.for_team,
            "team_id": str(event.team.id_uuid),
        }

    async def event_player_change(self, event: PlayerChange) -> dict:
        """Get the event for a player change.

        Args:
            event: The event to get.

        Returns:
            The event for a player change.

        """
        # calculate the time of the pauses before the event happened. By requesting the
        # pauses that are before the event and summing the length of the pauses
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event.time,
                start_time__gte=event.match_part.start_time,
            ),
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        time_in_minutes = round(
            (
                (event.time - event.match_part.start_time).total_seconds()
                + ((event.match_part.part_number - 1) * self.match_data.part_length)
                - pause_time
            )
            / 60,
        )

        left_over = time_in_minutes - (
            (event.match_part.part_number * self.match_data.part_length) / 60
        )
        if left_over > 0:
            time_in_minutes = (
                str(time_in_minutes - left_over).split(".")[0]
                + "+"
                + str(left_over).split(".")[0]
            )

        return {
            "type": "substitute",
            "name": "Wissel",
            "time": time_in_minutes,
            "player_in": event.player_in.user.username,
            "player_out": event.player_out.user.username,
            "player_group": str(event.player_group.id_uuid),
        }

    async def event_pause(self, event: Pause) -> dict:
        """Get the event for a pause.

        Args:
            event: The event to get.

        Returns:
            The event for a pause.

        """
        # calculate the time of the pauses before the event happened. By requesting the
        # pauses that are before the event and summing the length of the pauses
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event.start_time,
                start_time__gte=event.match_part.start_time,
            ),
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        # calculate the time in minutes sinds the real_start_time of the match and the
        # start_time of the pause
        time_in_minutes = round(
            (
                (event.start_time - event.match_part.start_time).total_seconds()
                + (
                    int(event.match_part.part_number - 1)
                    * int(self.match_data.part_length)
                )
                - pause_time
            )
            / 60,
        )

        left_over = time_in_minutes - (
            (event.match_part.part_number * self.match_data.part_length) / 60
        )
        if left_over > 0:
            time_in_minutes = (
                str(time_in_minutes - left_over).split(".")[0]
                + "+"
                + str(left_over).split(".")[0]
            )

        timeout = await Timeout.objects.filter(pause=event).afirst()

        return {
            "type": "intermission",
            "name": "Time-out" if timeout else "Pauze",
            "time": time_in_minutes,
            "length": event.length().total_seconds(),
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
        }

    @staticmethod
    async def check_access(user_id: UUID | None, match: Match) -> bool:
        """Check if the user has access to the match.

        Args:
            user_id: The id of the user.
            match: The match to check access for.

        Returns:
            True if the user has access, False otherwise.

        """
        if user_id is None:
            return False

        player = await Player.objects.aget(user=user_id)

        # Combine queries for players and coaches for both home and away teams
        team_data = await sync_to_async(list)(
            TeamData.objects.prefetch_related("players", "coach")
            .filter(Q(team=match.home_team) | Q(team=match.away_team))
            .values_list("players", "coach"),
        )

        # Flatten the list of tuples and remove None values
        players_list = [
            item for sublist in team_data for item in sublist if item is not None
        ]

        return player.id_uuid in players_list

    @staticmethod
    async def check_if_access(user_id: UUID | None, team: Team) -> bool:
        """Check if the user has access to the team.

        Args:
            user_id: The id of the user.
            team: The team to check access for.

        Returns:
            True if the user has access, False otherwise.

        """
        if user_id is None:
            return False

        player = await Player.objects.aget(user=user_id)

        players = await sync_to_async(list)(
            TeamData.objects.prefetch_related("players")
            .filter(team=team)
            .values_list("players", flat=True),
        )
        coaches = await sync_to_async(list)(
            TeamData.objects.prefetch_related("coach")
            .filter(team=team)
            .values_list("coach", flat=True),
        )

        players_list = []

        players_list.extend(players)
        players_list.extend(coaches)

        access = False
        if player.id_uuid in players_list:
            access = True

        return access

    async def send_data(self, event: dict) -> None:
        """Send data to the websocket.

        Args:
            event: The event to send.

        """
        data = event["data"]
        await self.send(text_data=json.dumps(data))

    async def season_request(self) -> Season | None:
        """Get the season of the match.

        Returns:
            The season of the match.

        """
        if self.match is None:
            return None

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
