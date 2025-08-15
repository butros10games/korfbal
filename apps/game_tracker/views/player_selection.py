"""Module contains the views for the player selection in the game tracker app."""

import json

from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render

from apps.game_tracker.models import MatchData, PlayerGroup
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


invalid_request = JsonResponse({"error": "Invalid request method"}, status=405)
json_error = JsonResponse({"error": "Invalid JSON data"}, status=400)
no_player_selected = JsonResponse({"error": "No player selected"}, status=400)
to_many_players = JsonResponse({"error": "Too many players selected"}, status=400)


def player_overview(request: HttpRequest, match_id: str, team_id: str) -> HttpResponse:
    """Render the player overview page.

    Args:
        request: The request object.
        match_id: The id of the match.
        team_id: The id of the team.

    Returns:
        The rendered player overview page.

    """
    player_groups = _get_player_groups(match_id, team_id)

    context = {
        "team_name": player_groups[0].team.__str__(),  # noqa: PLC2801
        "match_url": player_groups[0].match_data.match_link.get_absolute_url(),
        "player_groups": player_groups,
    }

    return render(request, "matches/players_selector.html", context)


def player_overview_data(_: HttpRequest, match_id: str, team_id: str) -> JsonResponse:
    """Get the player groups for a match and team.

    Args:
        _: The request object.
        match_id: The id of the match.
        team_id: The id of the team.

    Returns:
        The player groups for the match and team in JSON format.

    """
    player_groups = _get_player_groups(match_id, team_id)

    player_groups_data = []
    for player_group in player_groups:
        players_data = [
            {
                "id_uuid": str(player.id_uuid),
                "user": {"username": player.user.username},
                "get_profile_picture": player.get_profile_picture(),
            }
            for player in player_group.players.all()
        ]
        player_groups_data.append(
            {
                "id_uuid": str(player_group.id_uuid),
                "starting_type": {"name": player_group.starting_type.name},
                "players": players_data,
            },
        )

    return JsonResponse({"player_groups": player_groups_data})


def players_team(_: HttpRequest, match_id: str, team_id: str) -> JsonResponse:
    """Get the players that are not in a player group for a match and team.

    Args:
        _: The request object.
        match_id: The id of the match.
        team_id: The id of the team.

    Returns:
        The players that are not in a player group in JSON format.

    """
    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)

    team_data = TeamData.objects.get(team=team_model, season=match_data.season)

    # remove the players that are already in a player group
    players = team_data.players.all()
    player_groups = PlayerGroup.objects.filter(
        match_data=MatchData.objects.get(match_link=match_data),
        team=team_model,
    )

    for player_group in player_groups:
        players = players.exclude(id_uuid__in=player_group.players.all())

    return JsonResponse(
        {
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "user": {"username": player.user.username},
                    "get_profile_picture": player.get_profile_picture(),
                }
                for player in players
            ],
        },
    )


MIN_PLAYER_NAME_LENGTH = 3
MAX_PLAYER_NAME_LENGTH = 50


def player_search(request: HttpRequest, match_id: str, team_id: str) -> JsonResponse:
    """Search for players by name.

    Args:
        request: The request object.
        match_id: The id of the match.
        team_id: The id of the team.

    Returns:
        The players that match the search query in JSON format.

    """
    if request.method != "GET":
        return invalid_request

    # safely retrieve the search query with a default empty string
    search_query = request.GET.get("search", "")
    if not search_query:
        return no_player_selected

    if len(search_query) < MIN_PLAYER_NAME_LENGTH:
        return JsonResponse(
            {
                "success": False,
                "error": "Player name should be at least 3 characters long",
            },
        )

    if len(search_query) > MAX_PLAYER_NAME_LENGTH:
        return JsonResponse(
            {
                "success": False,
                "error": "Player name should be at most 50 characters long",
            },
        )

    match_data = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)

    # get the name of the player that is searched for
    player_name = search_query

    player_groups = PlayerGroup.objects.filter(
        match_data=MatchData.objects.get(match_link=match_data),
        team=team_model,
    )

    players = Player.objects.filter(user__username__icontains=player_name).exclude(
        id_uuid__in=[
            player.id_uuid
            for player_group in player_groups
            for player in player_group.players.all()
        ],
    )

    # return the players that are found in json format
    return JsonResponse(
        {
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "user": {"username": player.user.username},
                    "get_profile_picture": player.get_profile_picture(),
                }
                for player in players
            ],
        },
    )


MAX_RESERVE_PLAYERS = 16
MAX_STARTING_PLAYERS = 4


def player_designation(request: HttpRequest) -> JsonResponse:
    """Designate players to a player group.

    Args:
        request: The request object.

    Returns:
        The response to the request.

    """
    if request.method != "POST":
        return invalid_request

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return json_error

    selected_players = data.get("players", [])
    new_group_id = data.get("new_group_id")

    if not selected_players:
        return no_player_selected

    player_group_model = None
    if new_group_id:
        player_group_model = PlayerGroup.objects.get(id_uuid=new_group_id)

        if (
            player_group_model.starting_type.name == "Reserve"
            and len(selected_players) > MAX_RESERVE_PLAYERS
        ) or len(selected_players) > MAX_STARTING_PLAYERS:
            return to_many_players

    for player_data in selected_players:
        player_id = player_data.get("id_uuid")
        old_group_id = player_data.get("groupId")

        if old_group_id:
            PlayerGroup.objects.get(id_uuid=old_group_id).players.remove(
                Player.objects.get(id_uuid=player_id),
            )

        if player_group_model:
            player_group_model.players.add(Player.objects.get(id_uuid=player_id))

    return JsonResponse({"success": True})


def _get_player_groups(match_id: str, team_id: str) -> QuerySet[PlayerGroup]:
    """Get the player groups for a match and team.

    Args:
        match_id: The id of the match.
        team_id: The id of the team.

    Returns:
        The player groups for the match and team.

    """
    match_model = get_object_or_404(Match, id_uuid=match_id)
    team_model = get_object_or_404(Team, id_uuid=team_id)

    match_data = MatchData.objects.get(match_link=match_model)

    return PlayerGroup.objects.filter(match_data=match_data, team=team_model).order_by(
        "starting_type__order",
    )
