import locale
from datetime import datetime

from asgiref.sync import sync_to_async

from apps.game_tracker.models import Shot


async def transform_matchdata(matchs_data):
    match_dict = []
    locale.setlocale(locale.LC_TIME, "nl_NL.utf8")

    for match_data in matchs_data:
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
                "start_time": formatted_time,  # Add the time separately
                "length": match_data.part_lenght,
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
