"""M contains common functions for the game_tracker app consumers."""

from datetime import UTC, datetime
import json

from asgiref.sync import sync_to_async

from apps.game_tracker.models import MatchData, MatchPart, Pause


async def get_time(match_data: MatchData, current_part: MatchPart) -> str:
    """Get the time for the match.

    Args:
        match_data: The match data object.
        current_part: The current part of the match.

    Returns:
        The time for the match.

    """
    # check if there is a active part if there is a active part send the start time of
    # the part and length of a match part
    try:
        part = await MatchPart.objects.aget(match_data=match_data, active=True)
    except MatchPart.DoesNotExist:
        part = False

    if part:
        # check if there is a active pause if there is a active pause send the start
        # time of the pause
        try:
            active_pause = await Pause.objects.aget(
                match_data=match_data,
                active=True,
                match_part=current_part,
            )
        except Pause.DoesNotExist:
            active_pause = False

        # calculate all the time in pauses that are not active anymore
        pauses: list[Pause] = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=match_data,
                active=False,
                match_part=current_part,
            ),
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        if active_pause:
            return json.dumps(
                {
                    "command": "timer_data",
                    "type": "pause",
                    "match_data_id": str(match_data.id_uuid),
                    "time": part.start_time.isoformat(),
                    "calc_to": active_pause.start_time.isoformat(),
                    "length": match_data.part_length,
                    "pause_length": pause_time,
                    "server_time": datetime.now(UTC).isoformat(),
                },
            )
        return json.dumps(
            {
                "command": "timer_data",
                "type": "active",
                "match_data_id": str(match_data.id_uuid),
                "time": part.start_time.isoformat(),
                "length": match_data.part_length,
                "pause_length": pause_time,
                "server_time": datetime.now(UTC).isoformat(),
            },
        )
    return json.dumps(
        {
            "command": "timer_data",
            "type": "deactivated",
            "match_data_id": str(match_data.id_uuid),
        },
    )


def get_time_display(match_data: MatchData) -> str:
    """Get the time display for the match.

    Args:
        match_data: The match data object.

    Returns:
        The time display for the match.

    """
    time_left = match_data.part_length

    # convert the seconds to minutes and seconds to display on the page make the numbers
    # look nice with the %02d
    minutes = int(time_left / 60)
    seconds = int(time_left % 60)
    return f"{minutes:02d}:{seconds:02d}"


async def get_time_display_pause(self, json_data: dict) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    """Get the time display for the pause.

    Args:
        self: The instance of the class calling this method.
        json_data: A dictionary containing match data, including the match_data_id.

    """
    match_data = await MatchData.objects.prefetch_related("match_link").aget(
        id_uuid=json_data["match_data_id"],
    )

    current_part = await MatchPart.objects.aget(match_data=match_data, active=True)

    # Subscribe to time data channel
    if match_data.match_link.id_uuid not in self.subscribed_channels:
        await self.channel_layer.group_add(
            f"time_match_{match_data.match_link.id_uuid}",
            self.channel_name,
        )

        self.subscribed_channels.append(match_data.match_link.id_uuid)

    await self.send(text_data=await get_time(match_data, current_part))
