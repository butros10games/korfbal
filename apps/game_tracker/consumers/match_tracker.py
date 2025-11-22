"""Module contains the MatchTrackerConsumer class which is a websocket consumer."""

from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
import json
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Case, When
from django.utils import timezone

from apps.game_tracker.models import (
    Attack,
    GoalType,
    GroupType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    Shot,
    Timeout,
)
from apps.kwt_common.utils import get_time
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


class MatchTrackerConsumer(AsyncWebsocketConsumer):
    """A websocket consumer for the match tracker page."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Initialize the MatchTrackerConsumer."""
        super().__init__(*args, **kwargs)
        self.match: Match | None = None
        self.current_part: MatchPart | None = None
        self.is_paused = False
        self.match_is_paused_message = "match is paused"
        self.subscribed_channels: list[str] = []
        self.player_group_class = PlayerGroupClass(self._season_request)  # type: ignore[arg-type]

    async def connect(self) -> None:
        """Connect the websocket consumer."""
        match_id = self.scope["url_route"]["kwargs"]["id"]
        self.match = await Match.objects.prefetch_related(
            "home_team",
            "away_team",
        ).aget(id_uuid=match_id)
        self.match_data = await MatchData.objects.aget(match_link=self.match)
        self.team = await Team.objects.aget(
            id_uuid=self.scope["url_route"]["kwargs"]["team_id"],
        )
        with contextlib.suppress(MatchPart.DoesNotExist):
            self.current_part = await MatchPart.objects.aget(
                match_data=self.match_data,
                active=True,
            )

        # Check if an active pause exists for the given match_data
        is_pause_active = await sync_to_async(
            Pause.objects.filter(match_data=self.match_data, active=True).exists,
        )()

        # Set the pause status based on the existence of an active pause or the match
        # status
        self.is_paused = is_pause_active or self.match_data.status != "active"

        if self.team == self.match.home_team:
            self.other_team = self.match.away_team
        else:
            self.other_team = self.match.home_team

        self.channel_names = [
            f"detail_match_{self.match.id_uuid}",
            f"tracker_match_{self.match.id_uuid}",
            f"time_match_{self.match.id_uuid}",
        ]
        for channel_name in [self.channel_names[1], self.channel_names[2]]:
            await self.channel_layer.group_add(channel_name, self.channel_name)

        self.player_group_class.team = self.team
        self.player_group_class.match_data = self.match_data

        await self.accept()

        if self.match_data.status == "finished":
            await self.send(
                text_data=json.dumps(
                    {"command": "match_end", "match_id": str(self.match.id_uuid)},
                ),
            )

    async def disconnect(self, code: int) -> None:
        """Disconnect the websocket consumer."""
        await self.channel_layer.group_discard(self.channel_names[0], self.channel_name)
        await self.channel_layer.group_discard(self.channel_names[1], self.channel_name)
        await self.channel_layer.group_discard(self.channel_names[2], self.channel_name)

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        """Receive a message from the websocket.

        Args:
            text_data: The text data of the message.
            bytes_data: The bytes data of the message.

        """
        if bytes_data:
            text_data = bytes_data.decode("utf-8")
        if text_data is None:
            await self.send(text_data=json.dumps({"error": "No data received"}))
            return

        json_data: dict[str, Any] = json.loads(text_data)
        command: str = json_data["command"]

        await self.player_events(command=command, json_data=json_data)
        await self.game_events(command=command)
        await self.shot_events(command=command, json_data=json_data)

    async def player_events(self, command: str, json_data: dict[str, Any]) -> None:
        """Handle player events.

        Args:
            command (str): string with the command info in it.
            json_data (dict): Alle data form the websocket message in json format.

        """
        match command:
            case "playerGroups":
                await self.send(
                    text_data=await self.player_group_class.player_group_request(),
                )

            case "savePlayerGroups":
                await self.save_player_groups(json_data["playerGroups"])

            case "get_non_active_players":
                await self.get_non_active_players()

            case "substitute_reg":
                await self.substitute_reg(
                    json_data["new_player_id"],
                    json_data["old_player_id"],
                )

    async def game_events(self, command: str) -> None:
        """Handle game events.

        Args:
            command (str): string with the command info in it.

        """
        match command:
            case "start/pause":
                await self.start_pause()

            case "timeout":
                await self.timeout_reg()

            case "part_end":
                await self.part_end()

            case "get_time":
                if self.match_data and self.current_part:
                    await self.send(
                        text_data=await get_time(self.match_data, self.current_part),
                    )

            case "last_event":
                await self.send_last_event()

            case "remove_last_event":
                await self.removed_last_event()

            case "new_attack":
                await self.new_attack()

    async def shot_events(self, command: str, json_data: dict[str, Any]) -> None:
        """Handle shot events.

        Args:
            command (str): string with the command info in it.
            json_data (dict): Alle data form the websocket message in json format.

        """
        match command:
            case "shot_reg":
                await self.shot_reg(json_data["player_id"], json_data["for_team"])

            case "get_goal_types":
                await self.get_goal_types()

            case "goal_reg":
                await self.goal_reg(
                    json_data["player_id"],
                    json_data["goal_type"],
                    json_data["for_team"],
                )

    async def save_player_groups(self, player_groups: list[dict[str, Any]]) -> None:
        """Save the player groups to the database.

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

    async def shot_reg(self, player_id: UUID, for_team: bool) -> None:
        """Register a shot for a player.

        Args:
            player_id: The id of the player.
            for_team: A boolean indicating if the shot is for the team.

        """
        # check if the match is paused and if it is paused decline the request except
        # for the start/stop command
        if self.is_paused:
            await self.send(
                text_data=json.dumps(
                    {"command": "error", "error": self.match_is_paused_message},
                ),
            )
            return

        team = self.team if for_team else self.other_team

        await Shot.objects.acreate(
            player=await Player.objects.aget(id_uuid=player_id),
            match_data=self.match_data,
            match_part=self.current_part,
            time=datetime.now(UTC),
            for_team=for_team,
            team=team,
            scored=False,
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
                        team=self.team,
                    ).acount(),
                    "shots_against": await Shot.objects.filter(
                        player__id_uuid=player_id,
                        match_data=self.match_data,
                        team=self.other_team,
                    ).acount(),
                },
            },
        )

        await self.send_last_event()

    async def get_goal_types(self) -> None:
        """Get the goal types."""
        goal_type_list = await sync_to_async(list)(GoalType.objects.all())  # type: ignore[call-arg]

        goal_type_list = [
            {"id": str(goal_type.id_uuid), "name": goal_type.name}
            for goal_type in goal_type_list
        ]  # type: ignore[assignment]

        await self.send(
            text_data=json.dumps(
                {"command": "goal_types", "goal_types": goal_type_list},
            ),
        )

    async def goal_reg(
        self, player_id: UUID, goal_type_id: UUID, for_team: bool
    ) -> None:
        """Register a goal for a player.

        Args:
            player_id: The id of the player.
            goal_type_id: The id of the goal type.
            for_team: A boolean indicating if the goal is for the team.

        """
        # check if the match is paused and if it is paused decline the request except
        # for the start/stop command
        if self.is_paused:
            await self.send(
                text_data=json.dumps(
                    {"command": "error", "error": self.match_is_paused_message},
                ),
            )
            return

        team = self.team if for_team else self.other_team

        player = await Player.objects.prefetch_related("user").aget(id_uuid=player_id)
        goal_type_obj = await GoalType.objects.aget(id_uuid=goal_type_id)  # type: ignore[call-arg]

        await Shot.objects.acreate(
            player=await Player.objects.aget(id_uuid=player_id),
            match_data=self.match_data,
            match_part=self.current_part,
            time=datetime.now(UTC),
            shot_type=goal_type_obj,
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
                            team=self.team,
                        ).acount(),
                        "shots_against": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            team=self.other_team,
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
                        "goal_type": goal_type_obj.name,
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

            await self.channel_layer.group_send(
                channel_name,
                {
                    "type": "send_data",
                    "data": {
                        "command": "player_goal_change",
                        "player_id": player_id,
                        "goals_for": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            team=self.team,
                            scored=True,
                        ).acount(),
                        "goals_against": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            team=self.other_team,
                            scored=True,
                        ).acount(),
                    },
                },
            )

        number_of_shots = await Shot.objects.filter(
            match_data=self.match_data,
            scored=True,
        ).acount()
        if number_of_shots % 2 == 0:
            await self.player_group_class.swap_player_group_types(self.team)
            await self.player_group_class.swap_player_group_types(self.other_team)

            await self.send(
                text_data=await self.player_group_class.player_group_request(),
            )

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0],
            {"type": "get_events"},
        )

    async def timeout_reg(self) -> None:
        """Register a timeout."""
        if self.is_paused:
            await self.send(
                text_data=json.dumps(
                    {"command": "error", "error": self.match_is_paused_message},
                ),
            )
            return

        await self.start_pause()

        pause = await Pause.objects.aget(
            match_data=self.match_data,
            match_part=self.current_part,
            active=True,
        )

        await Timeout.objects.acreate(
            match_data=self.match_data,
            match_part=self.current_part,
            team=self.team,
            pause=pause,
        )

        await self.send_last_event()

    async def start_pause(self) -> None:
        """Start or pause the match."""
        try:
            part = await MatchPart.objects.aget(match_data=self.match_data, active=True)
        except MatchPart.DoesNotExist:
            part = await MatchPart.objects.acreate(
                match_data=self.match_data,
                active=True,
                start_time=datetime.now(UTC),
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
                    text_data=await self.player_group_class.player_group_request(),
                )

            time_message = await get_time(self.match_data, self.current_part)

            for channel_name in [self.channel_names[2]]:
                await self.channel_layer.group_send(
                    channel_name,
                    {
                        "type": "send_data",
                        "data": json.loads(time_message),
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
                    start_time=datetime.now(UTC),
                    match_part=self.current_part,
                )

                self.is_paused = True
            else:
                pause.active = False
                pause.end_time = datetime.now(UTC)
                await pause.asave()

                self.is_paused = False

            if self.match_data and self.current_part:
                pause_message = await get_time(self.match_data, self.current_part)
                pause_message = json.loads(pause_message)

            for channel_name in [self.channel_names[2]]:
                await self.channel_layer.group_send(
                    channel_name,
                    {"type": "send_data", "data": pause_message},
                )

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0],
            {"type": "get_events"},
        )

    async def part_end(self) -> None:
        """End the current part."""
        try:
            pause = await Pause.objects.aget(
                match_data=self.match_data,
                active=True,
                match_part=self.current_part,
            )

            pause.active = False
            pause.end_time = datetime.now(UTC)
            await pause.asave()

        except Pause.DoesNotExist:
            pass

        if self.match_data.current_part < self.match_data.parts:
            self.match_data.current_part += 1
            await self.match_data.asave()

            try:
                match_part = await MatchPart.objects.aget(
                    match_data=self.match_data,
                    active=True,
                )
                match_part.active = False
                match_part.end_time = datetime.now(UTC)
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
                            "part_length": self.match_data.part_length,
                            "match_data_id": str(self.match_data.id_uuid),
                        },
                    },
                )
        else:
            self.match_data.status = "finished"
            await self.match_data.asave()

            match_part = await MatchPart.objects.aget(
                match_data=self.match_data,
                active=True,
            )
            match_part.active = False
            match_part.end_time = datetime.now(UTC)
            await match_part.asave()

            for channel_name in [self.channel_names[2]]:
                await self.channel_layer.group_send(
                    channel_name,
                    {
                        "type": "send_data",
                        "data": {
                            "command": "match_end",
                            "match_id": str(self.match.id_uuid),  # type: ignore[union-attr]
                            "match_data_id": str(self.match_data.id_uuid),
                        },
                    },
                )

    async def get_non_active_players(self) -> None:
        """Get the players that are currently in the reserve player group."""
        reserve_group = await PlayerGroup.objects.prefetch_related(
            "players",
            "players__user",
        ).aget(
            match_data=self.match_data,
            team=self.team,
            starting_type__name="Reserve",
        )

        players_json = []
        for player in reserve_group.players.all():
            with contextlib.suppress(Player.DoesNotExist):
                players_json.append(
                    {"id": str(player.id_uuid), "name": player.user.username},
                )

        await self.send(
            text_data=json.dumps(
                {"command": "non_active_players", "players": players_json},
            ),
        )

    async def substitute_reg(self, new_player_id: UUID, old_player_id: UUID) -> None:
        """Register a player change.

        Args:
            new_player_id: The id of the new player.
            old_player_id: The id of the old player.

        """
        # check if the match is paused and if it is paused decline the request except
        # for the start/stop command
        if self.is_paused:
            await self.send(
                text_data=json.dumps(
                    {"command": "error", "error": self.match_is_paused_message},
                ),
            )
            return

        player_in = await Player.objects.prefetch_related("user").aget(
            id_uuid=new_player_id,
        )
        player_out = await Player.objects.prefetch_related("user").aget(
            id_uuid=old_player_id,
        )

        player_reserve_group = await PlayerGroup.objects.aget(
            team=self.team,
            match_data=self.match_data,
            starting_type__name="Reserve",
        )
        player_group = await PlayerGroup.objects.aget(
            team=self.team,
            match_data=self.match_data,
            players__in=[player_out],
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
            time=datetime.now(UTC),
        )

        # get the shot count for the new player
        shots_for = await Shot.objects.filter(
            player=player_in,
            match_data=self.match_data,
            for_team=True,
        ).acount()
        shots_against = await Shot.objects.filter(
            player=player_in,
            match_data=self.match_data,
            for_team=False,
        ).acount()

        goals_for = await Shot.objects.filter(
            player=player_in,
            match_data=self.match_data,
            for_team=True,
            scored=True,
        ).acount()
        goals_against = await Shot.objects.filter(
            player=player_in,
            match_data=self.match_data,
            for_team=False,
            scored=True,
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
                        "player_in_goals_for": goals_for,
                        "player_in_goals_against": goals_against,
                        "player_out": player_out.user.username,
                        "player_out_id": str(player_out.id_uuid),
                        "player_group": str(player_group.id_uuid),
                    },
                },
            )

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0],
            {"type": "get_events"},
        )

    async def removed_last_event(self) -> None:
        """Remove the last event."""
        event = await self._get_all_events()

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
                            team=self.team,
                        ).acount(),
                        "shots_against": await Shot.objects.filter(
                            player__id_uuid=player_id,
                            match_data=self.match_data,
                            team=self.other_team,
                        ).acount(),
                    },
                },
            )

            # check if the shot was a goal and if it was a goal check if it was a switch
            # goal and if it was a switch goal swap the player group types back
            if event.scored:
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
                    match_data=self.match_data,
                    scored=True,
                ).acount()
                if (number_of_shots) % 2 == 1:
                    await self.player_group_class.swap_player_group_types(self.team)
                    await self.player_group_class.swap_player_group_types(
                        self.other_team,
                    )

                    await self.send(
                        text_data=await self.player_group_class.player_group_request(),
                    )

        elif isinstance(event, PlayerChange):
            # get and delete the last player change event
            player_change = await PlayerChange.objects.prefetch_related(
                "player_group",
                "player_in",
                "player_out",
            ).aget(match_part=event.match_part, time=event.time)
            player_group = await PlayerGroup.objects.aget(
                id_uuid=player_change.player_group.id_uuid,
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
                text_data=await self.player_group_class.player_group_request(),
            )

        elif isinstance(event, Pause):
            # get and delete the last pause event
            pause = await Pause.objects.aget(
                active=event.active,
                match_part=event.match_part,
                start_time=event.start_time,
            )

            if event.active:
                await Timeout.objects.filter(pause=pause).adelete()
                await pause.adelete()
            else:
                pause.active = True
                pause.end_time = None
                await pause.asave()

            if self.match_data and self.current_part:
                time_data = await get_time(self.match_data, self.current_part)

            await self.channel_layer.group_send(
                self.channel_names[2],
                {"type": "send_data", "data": json.loads(time_data)},
            )

        elif isinstance(event, Attack):
            await event.adelete()

        await self.send_last_event()

        await self.channel_layer.group_send(
            self.channel_names[0],
            {"type": "get_events"},
        )

    async def _get_all_events(self) -> Shot | PlayerChange | Pause | Attack | None:
        """Get all events.

        Returns:
            The last event of the match, or None if there are no events.

        """
        # Fetch each type of event separately
        shots: list[Shot] = await sync_to_async(list)(  # type: ignore[call-arg]
            Shot.objects.prefetch_related(
                "player__user",
                "match_part",
                "shot_type",
                "match_data",
            )
            .filter(match_data=self.match_data)
            .order_by("time"),
        )
        player_changes: list[PlayerChange] = await sync_to_async(list)(  # type: ignore[call-arg]
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
            .order_by("time"),
        )
        time_outs: list[Pause] = await sync_to_async(list)(  # type: ignore[call-arg]
            Pause.objects.prefetch_related(
                "match_part",
                "match_part__match_data",
                "match_data__match_link",
            )
            .filter(match_data=self.match_data)
            .order_by("start_time"),
        )
        attacks: list[Attack] = await sync_to_async(list)(  # type: ignore[call-arg]
            Attack.objects.prefetch_related(
                "match_part",
                "match_data",
                "team",
                "team__club",
            )
            .filter(match_data=self.match_data)
            .order_by("time"),
        )

        events = sorted(
            shots + player_changes + time_outs + attacks,
            key=lambda x: getattr(
                x,
                "time",
                getattr(
                    x, "start_time", datetime.min.replace(tzinfo=timezone.now().tzinfo)
                ),
            ),
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
                    },
                ),
            )
            return None

        # Get last event
        return events[-1]

    async def send_last_event(self) -> None:
        """Send the last event."""
        last_event = await self._get_all_events()

        if last_event is None:
            return

        if isinstance(last_event, Shot):
            time_in_minutes = await self._time_calc(last_event)

            data_add = {"type": "shot", "name": "Schot"}
            if last_event.scored:
                goals_for = await Shot.objects.filter(
                    match_data=self.match_data,
                    for_team=True,
                    scored=True,
                ).acount()
                goals_against = await Shot.objects.filter(
                    match_data=self.match_data,
                    for_team=False,
                    scored=True,
                ).acount()

                data_add = {
                    "type": "goal",
                    "name": "punt",
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

            time_in_minutes = await self._time_calc(last_event)

            events_dict = {
                "type": "substitute",
                "name": "Wissel",
                "time": time_in_minutes,
                "player_in": player_in_username,
                "player_out": player_out_username,
                "player_group": str(last_event.player_group.id_uuid),
            }
        elif isinstance(last_event, Pause):
            time_in_minutes = await self._time_calc(last_event)

            timeout = await Timeout.objects.filter(pause=last_event).afirst()

            events_dict = {
                "type": "pause",
                "name": "Time-out" if timeout else "Pauze",
                "time": time_in_minutes,
                "length": last_event.length().total_seconds(),
                "start_time": (
                    last_event.start_time.isoformat() if last_event.start_time else None
                ),
                "end_time": (
                    last_event.end_time.isoformat() if last_event.end_time else None
                ),
            }
        elif isinstance(last_event, Attack):
            time_in_minutes = await self._time_calc(last_event)

            events_dict = {
                "type": "attack",
                "name": "Aanval",
                "time": time_in_minutes,
                "team": last_event.team.__str__(),  # noqa: PLC2801
            }

        await self.send(
            text_data=json.dumps({"command": "last_event", "last_event": events_dict}),
        )

    async def _time_calc(self, event: Shot | PlayerChange | Pause | Attack) -> str:
        """Calculate the time of the event.

        Args:
            event: The event to calculate the time for.

        Returns:
            The time of the event.

        Raises:
            ValueError: If the event does not have a time or start_time attribute.

        """
        # Determine the event time attribute, either "time" or "start_time"
        event_time = getattr(event, "time", getattr(event, "start_time", None))

        if not event_time:
            raise ValueError("Event must have either `time` or `start_time` attribute")

        # Calculate the time of the pauses before the event happened by summing the
        # length of the pauses
        pauses: list[Pause] = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=self.match_data,
                active=False,
                start_time__lt=event_time,
                start_time__gte=event.match_part.start_time,
            ),
        )  # type: ignore[call-arg]
        pause_time = sum(pause.length().total_seconds() for pause in pauses)

        # Calculate the time in minutes since the real_start_time of the match and the
        # event time
        time_in_minutes = round(
            (
                (event_time - event.match_part.start_time).total_seconds()
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

        return str(time_in_minutes)

    # is for the data that needs be send from other websocket connections
    async def send_data(self, event: dict[str, Any]) -> None:
        """Send data to the websocket."""
        data = event["data"]
        await self.send(text_data=json.dumps(data))

    async def _season_request(self) -> Season:
        """Get the season for the match.

        Returns:
            The season for the match.

        Raises:
            ValueError: If data_resolution or data_points is None.

        """
        if not self.match:
            raise ValueError

        try:
            return await Season.objects.aget(
                start_date__lte=self.match.start_time,
                end_date__gte=self.match.start_time,
            )
        except Season.DoesNotExist:
            season_data = await sync_to_async(
                Season.objects.filter(end_date__lte=self.match.start_time)
                .order_by("-end_date")
                .first,
            )()

        if not season_data:
            raise ValueError

        return season_data

    async def new_attack(self) -> None:
        """Start a new attack."""
        if self.is_paused:
            await self.send(
                text_data=json.dumps(
                    {"command": "error", "error": self.match_is_paused_message},
                ),
            )
            return

        await Attack.objects.acreate(
            match_data=self.match_data,
            match_part=self.current_part,
            team=self.team,
            time=datetime.now(UTC),
        )

        await self.send_last_event()


class PlayerGroupClass:
    """A class for the player groups."""

    def __init__(self, _season_request: Callable[[], Any] | None = None) -> None:  # type: ignore[arg-type]
        """Initialize the PlayerGroupClass."""
        self.team = None
        self.match_data = None
        self.__season_request: Callable[[], Any] | None = _season_request  # type: ignore[assignment]

    async def player_group_request(self) -> str:
        """Get the player groups.

        Returns:
            The player groups.

        """
        player_groups_array = await self._make_player_group_list()

        return json.dumps(
            {
                "command": "playerGroups",
                "playerGroups": player_groups_array,
                "players": await self._make_full_player_list(),
                "match_active": self.match_data.status == "active",  # type: ignore[attr-defined]
            },
        )

    async def get_player_groups(self) -> list[PlayerGroup]:
        """Get the player groups.

        Returns:
            The player groups.

        """
        return await sync_to_async(list)(  # type: ignore[call-arg]
            PlayerGroup.objects.prefetch_related(
                "players",
                "players__user",
                "starting_type",
                "current_type",
            )
            .filter(match_data=self.match_data, team=self.team)
            .exclude(starting_type__name="Reserve")
            .order_by(
                Case(When(current_type__name="Aanval", then=0), default=1),
                "starting_type",
            ),
        )

    async def swap_player_group_types(self, team: Team) -> None:
        """Swap the player group types.

        Args:
            team: The team to swap the player group types for.

        """
        group_type_a = await GroupType.objects.aget(name="Aanval")
        group_type_v = await GroupType.objects.aget(name="Verdediging")

        player_group_a = await PlayerGroup.objects.aget(
            match_data=self.match_data,
            team=team,
            current_type=group_type_a,
        )
        player_group_v = await PlayerGroup.objects.aget(
            match_data=self.match_data,
            team=team,
            current_type=group_type_v,
        )

        player_group_a.current_type = group_type_v
        player_group_v.current_type = group_type_a

        await player_group_a.asave()
        await player_group_v.asave()

    async def _make_player_group_list(self) -> list[dict[str, Any]]:
        """Make a list of player groups.

        Returns:
            A list of player groups.

        """
        player_groups = await self.get_player_groups()
        if not player_groups:
            await self._create_player_groups()
            player_groups = await self.get_player_groups()
        return await self._make_player_group_json(player_groups)

    async def _create_player_groups(self) -> None:
        """Create the player groups."""
        group_types: list[GroupType] = await sync_to_async(list)(
            GroupType.objects.all().order_by("id")
        )  # type: ignore[call-arg]
        for group_type in group_types:
            await PlayerGroup.objects.acreate(
                match_data=self.match_data,
                team=self.team,
                starting_type=group_type,
                current_type=group_type,
            )

    async def _make_player_group_json(
        self, player_groups: list[PlayerGroup]
    ) -> list[dict[str, Any]]:
        """Make a list of player groups in JSON format.

        Args:
            player_groups: The player groups to convert to JSON format.

        Returns:
            A list of player groups in JSON format.

        """

        async def _process_player(player: Player) -> dict[str, Any]:
            return {
                "id": str(player.id_uuid),
                "name": player.user.username,
                "shots_for": await Shot.objects.filter(
                    player=player,
                    match_data=self.match_data,
                    for_team=True,
                ).acount(),
                "goals_for": await Shot.objects.filter(
                    player=player,
                    match_data=self.match_data,
                    for_team=True,
                    scored=True,
                ).acount(),
                "shots_against": await Shot.objects.filter(
                    player=player,
                    match_data=self.match_data,
                    for_team=False,
                ).acount(),
                "goals_against": await Shot.objects.filter(
                    player=player,
                    match_data=self.match_data,
                    for_team=False,
                    scored=True,
                ).acount(),
            }

        async def _process_player_group(player_group: PlayerGroup) -> dict[str, Any]:
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

    async def _make_full_player_list(self) -> list[dict[str, Any]]:
        """Make a list of all players in the team.

        Returns:
            A list of all players in the team.

        Raises:
            RuntimeError: If season_request is not initialized.

        """
        if self.__season_request is None:
            raise RuntimeError("season_request not initialized")
        season = await self.__season_request()  # type: ignore[misc]
        players_json = []
        players: list[Player] = await sync_to_async(list)(  # type: ignore[call-arg]
            TeamData.objects.prefetch_related("players")
            .filter(team=self.team, season=season)
            .values_list("players", flat=True),
        )

        for player in players:
            player_json = await self._get_player_json(player)
            if player_json:
                players_json.append(player_json)

        return self._remove_duplicates(players_json)

    @staticmethod
    async def _get_player_json(player: Player) -> dict[str, Any] | None:
        """Get a player in JSON format.

        Args:
            player: The player to get in JSON format.

        Returns:
            The player in JSON format.

        """
        try:
            player_obj = await Player.objects.prefetch_related("user").aget(
                id_uuid=player,
            )
            return {
                "id": str(player_obj.id_uuid),
                "name": player_obj.user.username,
                "profile_picture": player_obj.get_profile_picture(),
                "get_absolute_url": str(player_obj.get_absolute_url()),
            }
        except Player.DoesNotExist:
            return None

    @staticmethod
    def _remove_duplicates(players_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate players from the list.

        Args:
            players_json: The list of players to remove duplicates from.

        Returns:
            The list of players without duplicates.

        """
        return [dict(t) for t in {tuple(d.items()) for d in players_json}]
