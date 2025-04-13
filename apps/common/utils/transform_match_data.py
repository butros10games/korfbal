"""This module contains the function to transform match data to a dictionary."""

from datetime import datetime
import locale

from asgiref.sync import sync_to_async

from apps.game_tracker.models import Shot

from .time_utils import get_time_display


async def transform_match_data(matches_data: list) -> list:
    """Transform the match data to a dictionary.

    Args:
        matches_data {list} -- A list of match data.

    Returns:
        list -- A list of dictionaries containing the match data.

    """
    match_dict = []
    locale.setlocale(locale.LC_TIME, "nl_NL.utf8")

    for match_data in matches_data:
        start_time_dt = datetime.fromisoformat(
            match_data.match_link.start_time.isoformat()
        )

        # Format the date as "za 01 april"
        formatted_date = start_time_dt.strftime(
            "%a %d %b"
        ).lower()  # %a for abbreviated day name

        # Extract the time as "14:45"
        formatted_time = start_time_dt.strftime("%H:%M")

        home_team = match_data.match_link.home_team
        away_team = match_data.match_link.away_team

        match_dict.append(
            {
                "id_uuid": str(match_data.match_link.id_uuid),
                "match_data_id": str(match_data.id_uuid),
                "home_team": await sync_to_async(home_team.__str__)(),
                "home_team_logo": home_team.club.get_club_logo(),
                "home_score": await Shot.objects.filter(
                    match_data=match_data, team=home_team, scored=True
                ).acount(),
                "away_team": await sync_to_async(away_team.__str__)(),
                "away_team_logo": away_team.club.get_club_logo(),
                "away_score": await Shot.objects.filter(
                    match_data=match_data, team=away_team, scored=True
                ).acount(),
                "start_date": formatted_date,
                "start_time": formatted_time,
                "current_part": match_data.current_part,
                "parts": match_data.parts,
                "length": match_data.part_length,
                "time_display": get_time_display(match_data),
                "status": match_data.status,
                "winner": (
                    await sync_to_async(match_data.get_winner().__str__)()
                    if match_data.get_winner()
                    else None
                ),
                "get_absolute_url": str(match_data.match_link.get_absolute_url()),
            }
        )

    return match_dict
