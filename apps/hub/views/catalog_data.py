from django.db.models import Q
from django.http import JsonResponse

from apps.player.models import Player
from apps.team.models import Team
from apps.club.models import Club

import json


def catalog_data(request):
    connected_list = []
    following_list = []
    selection = None

    user = request.user

    if request.method == 'POST':
        data = json.loads(request.body.decode('utf-8'))

        if 'value' in data:
            selection = data['value']

            if selection in ["clubs", "teams"] and user.is_authenticated:
                player = Player.objects.get(user=user)
                SELECTION_MAP = {
                    'clubs': {
                        'connected_query': connected_clubs_query,
                        'following_relation': 'club_follow',
                        'serializer_func': club_serializer,
                    },
                    'teams': {
                        'connected_query': connected_teams_query,
                        'following_relation': 'team_follow',
                        'serializer_func': team_serializer,
                    },
                }
                mapping = SELECTION_MAP.get(selection)
                if mapping:
                    connected_list, following_list = get_connected_and_following_objects(
                        player,
                        mapping['connected_query'],
                        mapping['following_relation'],
                        mapping['serializer_func']
                    )

    context = {
        "type": selection,
        "connected": connected_list,
        "following": following_list
    }

    return JsonResponse(context)

def connected_clubs_query(player):
    return Club.objects.filter(
        Q(teams__team_data__players=player) | Q(teams__team_data__coach=player)
    ).distinct()

def club_serializer(club):
    return {
        "id": str(club.id_uuid),
        "name": club.name,
        "img_url": club.logo.url if club.logo else None,
        "competition": None,
        "url": str(club.get_absolute_url())
    }

def connected_teams_query(player):
    return Team.objects.filter(
        Q(team_data__players=player) | Q(team_data__coach=player)
    ).distinct()

def team_serializer(team):
    return {
        "id": str(team.id_uuid),
        "name": str(team),
        "img_url": team.club.logo.url if team.club.logo else None,
        "competition": team.team_data.last().competition if team.team_data else "",
        "url": str(team.get_absolute_url())
    }

def get_connected_and_following_objects(player, connected_query, following_relation, serializer_func):
    connected_objs = connected_query(player)
    following_objs = getattr(player, following_relation).exclude(id_uuid__in=connected_objs)
    
    connected_list = [serializer_func(obj) for obj in connected_objs]
    following_list = [serializer_func(obj) for obj in following_objs]
    
    return connected_list, following_list