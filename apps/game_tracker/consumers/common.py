import json

from asgiref.sync import sync_to_async

from apps.game_tracker.models import MatchPart, Pause


async def get_time(match_data, current_part):
    # check if there is a active part if there is a active part send the start time of
    # the part and lenght of a match part
    try:
        part = await MatchPart.objects.aget(match_data=match_data, active=True)
    except MatchPart.DoesNotExist:
        part = False

    if part:
        # check if there is a active pause if there is a active pause send the start
        # time of the pause
        try:
            active_pause = await Pause.objects.aget(
                match_data=match_data, active=True, match_part=current_part
            )
        except Pause.DoesNotExist:
            active_pause = False

        # calculate all the time in pauses that are not active anymore
        pauses = await sync_to_async(list)(
            Pause.objects.filter(
                match_data=match_data, active=False, match_part=current_part
            )
        )
        pause_time = 0
        for pause in pauses:
            pause_time += pause.length().total_seconds()

        if active_pause:
            return json.dumps(
                {
                    "command": "timer_data",
                    "type": "pause",
                    "time": part.start_time.isoformat(),
                    "calc_to": active_pause.start_time.isoformat(),
                    "length": match_data.part_lenght,
                    "pause_length": pause_time,
                }
            )
        else:
            return json.dumps(
                {
                    "command": "timer_data",
                    "type": "active",
                    "time": part.start_time.isoformat(),
                    "length": match_data.part_lenght,
                    "pause_length": pause_time,
                }
            )
    else:
        return json.dumps({"command": "timer_data", "type": "deactive"})
