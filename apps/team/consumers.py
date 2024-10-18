from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q

from apps.game_tracker.models import Shot, GoalType, MatchData
from apps.player.models import Player
from apps.schedule.models import Season, Match
from apps.team.models import Team, TeamData

from apps.hub.utils.transform_matchdata import transform_matchdata

import json
import traceback
import locale
from datetime import datetime

class team_data(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.team = None
    
    async def connect(self):
        team_id = self.scope['url_route']['kwargs']['id']
        self.team = await sync_to_async(Team.objects.get)(id_uuid=team_id)
        await self.accept()
        
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data['command']
            
            if command == "wedstrijden" or command == "ended_matches":
                await self.matches_request(command)
                
            elif command == "team_stats":
                data_type = json_data['data_type']
                
                if data_type == 'general':
                    await self.team_stats_general_request()
                
                elif data_type == 'player_stats':
                    await self.team_stats_player_request()
                
            elif command == "spelers":
                await self.player_request(json_data)
                
            elif command == "follow":
                await self.follow_request(json_data['followed'], json_data['user_id'])
                    
        except Exception as e:
            await self.send(text_data=json.dumps({
                'error': str(e),
                'traceback': traceback.format_exc()
            }))
            
    async def matches_request(self, command):
        wedstrijden_data = await self.get_matchs_data(
            ['upcoming', 'active'] if command == "wedstrijden" else ['finished'],
            '' if command == "wedstrijden" else '-'
        )
        
        wedstrijden_dict = await transform_matchdata(wedstrijden_data)
        
        await self.send(text_data=json.dumps({
            'command': 'wedstrijden',
            'wedstrijden': wedstrijden_dict
        }))
        
    async def team_stats_general_request(self):
        ## get the amount of goals for and against for all the types
        goal_types = await sync_to_async(list)(GoalType.objects.all())
        
        goal_types_json = [
            {
                'id': str(goal_type.id_uuid),
                'name': goal_type.name
            }
            for goal_type in goal_types
        ]
        
        # get a list of all the matches of the team
        matches = await sync_to_async(list)(Match.objects.filter(Q(home_team=self.team) | Q(away_team=self.team)))
        
        match_datas = await sync_to_async(list)(MatchData.objects.filter(match_link__in=matches))
        
        team_goal_stats = {}
        for goal_type in goal_types:
            goals_for = 0
            goals_against = 0
            
            goals_for += await sync_to_async(Shot.objects.filter(match_data__in=match_datas, shot_type=goal_type, for_team=True, scored=True).count)()
            goals_against += await sync_to_async(Shot.objects.filter(match_data__in=match_datas, shot_type=goal_type, for_team=False, scored=True).count)()
            
            team_goal_stats[goal_type.name] = {
                "goals_by_player": goals_for,
                "goals_against_player": goals_against
            }
        
        shots_for = await sync_to_async(Shot.objects.filter(match_data__in=match_datas, for_team=True).count)()
        shots_against = await sync_to_async(Shot.objects.filter(match_data__in=match_datas, for_team=False).count)()
        goals_for = await sync_to_async(Shot.objects.filter(match_data__in=match_datas, for_team=True, scored=True).count)()
        goals_against = await sync_to_async(Shot.objects.filter(match_data__in=match_datas, for_team=False, scored=True).count)()
        
        await self.send(text_data=json.dumps({
            'command': 'goal_stats',
            'data': {
                'type': 'general',
                'stats': {
                    'shots_for': shots_for,
                    'shots_against': shots_against,
                    'goals_for': goals_for,
                    'goals_against': goals_against,
                    'team_goal_stats': team_goal_stats,
                    'goal_types': goal_types_json,
                }
            }
        }))
        
    async def team_stats_player_request(self):
        # Fetch players and matches in bulk
        players = await sync_to_async(list)(
            Player.objects.prefetch_related('user')
            .filter(team_data_as_player__team=self.team)
            .distinct()
        )

        matches = await sync_to_async(list)(
            Match.objects.filter(Q(home_team=self.team) | Q(away_team=self.team))
        )
        
        match_datas = await sync_to_async(list)(MatchData.objects.filter(match_link__in=matches))
        
        # Fetch all shots in bulk
        shots = await sync_to_async(list)(
            Shot.objects.filter(
                match_data__in=match_datas, 
                player__in=players
            ).select_related('match_data', 'player')
        )

        players_stats = []

        # Process data in Python instead of making separate DB queries for each player and match
        for player in players:
            player_shots = [shot for shot in shots if shot.player_id == player.id_uuid]
            shots_for = sum(1 for shot in player_shots if shot.for_team)
            shots_against = sum(1 for shot in player_shots if not shot.for_team)
            goals_for = sum(1 for shot in player_shots if shot.for_team and shot.scored)
            goals_against = sum(1 for shot in player_shots if not shot.for_team and shot.scored)

            player_stats = {
                'username': player.user.username,
                'shots_for': shots_for,
                'shots_against': shots_against,
                'goals_for': goals_for,
                'goals_against': goals_against,
            }
            
            players_stats.append(player_stats)

        # Sort players_stats
        players_stats.sort(key=lambda x: x['goals_for'], reverse=True)
        
        # remove all the players with no goals
        players_stats = [player for player in players_stats if player['goals_for'] > 0]

        # Prepare and send data
        await self.send(text_data=json.dumps({
            'command': 'goal_stats',
            'data': {
                'type': 'player_stats',
                'stats': {
                    'player_stats': players_stats
                }
            }
        }))
        
    async def player_request(self, json_data):
        if 'season_uuid' in json_data:
            season_uuid = json_data['season_uuid']
        else:
            season_uuid = None
        
        # Initialize an empty list to store players
        players_in_team_season = []

        # Check if a specific season is provided
        if season_uuid:
            # Assuming you have a Season object or its UUID
            season = await sync_to_async(Season.objects.get)(id_uuid=season_uuid)

            # Get all TeamData instances for the specified team and season
            team_data_instances = await sync_to_async(list)(TeamData.objects.prefetch_related('players').filter(team=self.team, season=season))

            # Iterate through the TeamData instances and collect players
            for team_data_instance in team_data_instances:
                all_players = await sync_to_async(team_data_instance.players.all)()
                players_prefetch = await sync_to_async(all_players.prefetch_related)('user')
                await sync_to_async(players_in_team_season.extend)(players_prefetch)
        else:
            # retreve the players of the current season or last season if there is no current season
            try:
                current_season = await sync_to_async(Season.objects.get)(start_date__lte=datetime.now(), end_date__gte=datetime.now())
            except Season.DoesNotExist:
                current_season = await sync_to_async(Season.objects.filter(end_date__lte=datetime.now()).order_by('-end_date').first)()
            
            # Get the team data instances for the current season
            all_team_data_instances = await sync_to_async(list)(TeamData.objects.prefetch_related('players').filter(team=self.team, season=current_season))

            # Iterate through all TeamData instances and collect players
            for team_data_instance in all_team_data_instances:
                all_players = await sync_to_async(team_data_instance.players.all)()
                players_prefetch = await sync_to_async(all_players.prefetch_related)('user')
                await sync_to_async(players_in_team_season.extend)(players_prefetch)
                
        players_in_team_season_list = await sync_to_async(list)(players_in_team_season)
                
        players_in_team_season_dict = [
            {
                'id': str(player.id_uuid),
                'name': player.user.username,
                'profile_picture': ('/media' if 'static' not in player.profile_picture.url else '') + player.profile_picture.url if player.profile_picture else None,
                'get_absolute_url': str(player.get_absolute_url())
            }
            for player in players_in_team_season_list
        ]

        await self.send(text_data=json.dumps({
            'command': 'spelers',
            'spelers': players_in_team_season_dict,
        }))
    
    async def follow_request(self, follow, user_id):
        player = await Player.objects.aget(user=user_id)
        
        if follow:
            await player.team_follow.aadd(self.team)
            
        else:
            await player.team_follow.aremove(self.team)
        
        await self.send(text_data=json.dumps({
            'command': 'follow',
            'status': 'success'
        }))
    
    async def get_matchs_data(self, status, order):
        matches = await sync_to_async(list)(Match.objects.filter(
           Q(home_team=self.team) | 
           Q(away_team=self.team)
        ).distinct())
        
        matches_non_dub = list(dict.fromkeys(matches))
        
        matchs_data = await sync_to_async(list)(MatchData.objects.prefetch_related(
            'match_link', 
            'match_link__home_team', 
            'match_link__home_team__club', 
            'match_link__away_team', 
            'match_link__away_team__club'
        ).filter(match_link__in=matches_non_dub, status__in=status).order_by(order + "match_link__start_time"))
        
        return matchs_data
