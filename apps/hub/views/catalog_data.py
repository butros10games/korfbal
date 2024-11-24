"""This module contains the view for fetching data for the catalog page."""

import json

from apps.club.models import Club
from apps.player.models import Player
from apps.team.models import Team
from django.db.models import Q
from django.http import JsonResponse


def catalog_data(request):
    """
    View for fetching data for the catalog page.

    Args:
        request (HttpRequest): The request object.

    Returns:
        JsonResponse: The response object.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"})

    connected_list = []
    following_list = []
    selection = None

    user = request.user
    data = json.loads(request.body.decode("utf-8"))

    if "value" not in data:
        return JsonResponse({"error": "No value provided"})

    selection = data["value"]

    if selection in ["clubs", "teams"] and user.is_authenticated:
        player = Player.objects.get(user=user)
        SELECTION_MAP = {
            "clubs": {
                "connected_query": connected_clubs_query,
                "following_relation": "club_follow",
                "serializer_func": club_serializer,
            },
            "teams": {
                "connected_query": connected_teams_query,
                "following_relation": "team_follow",
                "serializer_func": team_serializer,
            },
        }
        mapping = SELECTION_MAP.get(selection)
        if mapping:
            connected_list, following_list = get_connected_and_following_objects(
                player,
                mapping["connected_query"],
                mapping["following_relation"],
                mapping["serializer_func"],
            )

    context = {
        "type": selection,
        "connected": connected_list,
        "following": following_list,
    }

    return JsonResponse(context)


def connected_clubs_query(player):
    """
    Get the clubs the player is connected to.

    Args:
        player (Player): The player object.

    Returns:
        QuerySet: The queryset of the clubs the player is connected to.
    """
    return Club.objects.filter(
        Q(teams__team_data__players=player) | Q(teams__team_data__coach=player)
    ).distinct()


def club_serializer(club):
    """
    Serialize the club object.

    Args:
        club (Club): The club object.

    Returns:
        dict: The serialized club object.
    """
    return {
        "id": str(club.id_uuid),
        "name": club.name,
        "img_url": club.get_club_logo(),
        "competition": None,
        "url": str(club.get_absolute_url()),
    }


def connected_teams_query(player):
    """
    Get the teams the player is connected to.

    Args:
        player (Player): The player object.

    Returns:
        QuerySet: The queryset of the teams the player is connected to.
    """
    return Team.objects.filter(
        Q(team_data__players=player) | Q(team_data__coach=player)
    ).distinct()


def team_serializer(team):
    """
    Serialize the team object.

    Args:
        team (Team): The team object.

    Returns:
        dict: The serialized team object.
    """
    last_team_data = team.team_data.last() if team.team_data else None
    return {
        "id": str(team.id_uuid),
        "name": str(team),
        "img_url": team.club.get_club_logo(),
        "competition": last_team_data.competition if last_team_data else "",
        "url": str(team.get_absolute_url()),
    }


def get_connected_and_following_objects(
    player, connected_query, following_relation, serializer_func
):
    """
    Get the connected and following objects for the player.

    Args:
        player (Player): The player object.
        connected_query (function): The function to get the connected objects.
        following_relation (str): The name of the following relation.
        serializer_func (function): The function to serialize the object.

    Returns:
        tuple: The connected and following objects.
    """
    connected_objs = connected_query(player)
    following_objs = getattr(player, following_relation).exclude(
        id_uuid__in=connected_objs
    )

    connected_list = [serializer_func(obj) for obj in connected_objs]
    following_list = [serializer_func(obj) for obj in following_objs]

    return connected_list, following_list
