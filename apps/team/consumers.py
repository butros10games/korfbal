import json
import traceback
import locale
from datetime import datetime

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from django.db.models import Q
from apps.game_tracker.models import Shot, GoalType
from apps.player.models import Player
from apps.schedule.models import Season, Match
from apps.team.models import Team, TeamData

class team_data(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.team = None
    
    async def connect(self):
        team_id = self.scope['url_route']['kwargs']['id']
        self.team = await sync_to_async(Team.objects.get)(id_uuid=team_id)
        await self.accept()
        
    async def disconnect(self, close_code):
        pass
        
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data['command']
            
            if command == "wedstrijden":
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'home_team__club', 'away_team', 'away_team__club').filter(Q(home_team=self.team) | Q(away_team=self.team), finished=False).order_by('start_time'))
                
                wedstrijden_dict = await transfrom_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'wedstrijden',
                    'wedstrijden': wedstrijden_dict
                }))
                
            elif command == "ended_matches":
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'home_team__club', 'away_team', 'away_team__club').filter(Q(home_team=self.team) | Q(away_team=self.team), finished=True).order_by('-start_time'))
                
                wedstrijden_dict = await transfrom_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'wedstrijden',
                    'wedstrijden': wedstrijden_dict
                }))
                
            elif command == "team_stats":
                data_type = json_data['data_type']
                
                if data_type == 'general':
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
                    
                    team_goal_stats = {}
                    for goal_type in goal_types:
                        goals_for = 0
                        goals_against = 0
                        
                        for match in matches:
                            goals_for += await sync_to_async(Shot.objects.filter(match=match, shot_type=goal_type, for_team=True, scored=True).count)()
                            goals_against += await sync_to_async(Shot.objects.filter(match=match, shot_type=goal_type, for_team=False, scored=True).count)()
                        
                        team_goal_stats[goal_type.name] = {
                            "goals_by_player": goals_for,
                            "goals_against_player": goals_against
                        }
                        
                    shots_for = 0
                    shots_against = 0
                    goals_for = 0
                    goals_against = 0
                    
                    for match in matches:
                        shots_for += await sync_to_async(Shot.objects.filter(match=match, for_team=True).count)()
                        shots_against += await sync_to_async(Shot.objects.filter(match=match, for_team=False).count)()
                        goals_for += await sync_to_async(Shot.objects.filter(match=match, for_team=True, scored=True).count)()
                        goals_against += await sync_to_async(Shot.objects.filter(match=match, for_team=False, scored=True).count)()
                    
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
                
                elif data_type == 'player_stats':
                    # Fetch players and matches in bulk
                    players = await sync_to_async(list)(
                        Player.objects.prefetch_related('user')
                        .filter(team_data_as_player__team=self.team)
                        .distinct()
                    )

                    matches = await sync_to_async(list)(
                        Match.objects.filter(Q(home_team=self.team) | Q(away_team=self.team))
                    )

                    # Fetch all shots in bulk
                    shots = await sync_to_async(list)(
                        Shot.objects.filter(
                            match__in=matches, 
                            player__in=players
                        ).select_related('match', 'player')
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

                
            elif command == "spelers":
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
                        'profile_picture': player.profile_picture.url if player.profile_picture else None,
                        'get_absolute_url': str(player.get_absolute_url())
                    }
                    for player in players_in_team_season_list
                ]

                await self.send(text_data=json.dumps({
                    'command': 'spelers',
                    'spelers': players_in_team_season_dict,
                }))
                
            elif command == "follow":
                follow = json_data['followed']
                user_id = json_data['user_id']
                
                player = await sync_to_async(Player.objects.get)(user=user_id)
                
                if follow:
                    await sync_to_async(player.team_follow.add)(self.team)
                    
                else:
                    await sync_to_async(player.team_follow.remove)(self.team)
                
                await self.send(text_data=json.dumps({
                    'command': 'follow',
                    'status': 'success'
                }))
                    
        except Exception as e:
            await self.send(text_data=json.dumps({
                'error': str(e),
                'traceback': traceback.format_exc()
            }))
            
async def transfrom_matchdata(wedstrijden_data):
    wedstrijden_dict = []
            
    for wedstrijd in wedstrijden_data:
        locale.setlocale(locale.LC_TIME, 'nl_NL.utf8')
        start_time_dt = datetime.fromisoformat(wedstrijd.start_time.isoformat())
        
        # Format the date as "za 01 april"
        formatted_date = start_time_dt.strftime("%a %d %b").lower()  # %a for abbreviated day name

        # Extract the time as "14:45"
        formatted_time = start_time_dt.strftime("%H:%M")

        wedstrijden_dict.append({
            'id_uuid': str(wedstrijd.id_uuid),
            'home_team': await sync_to_async(wedstrijd.home_team.__str__)(),
            'home_team_logo': wedstrijd.home_team.club.logo.url if wedstrijd.home_team.club.logo else None,
            'home_score': await sync_to_async(Shot.objects.filter(match=wedstrijd, team=wedstrijd.home_team, scored=True).count)(),
            'away_team': await sync_to_async(wedstrijd.away_team.__str__)(),
            'away_team_logo': wedstrijd.away_team.club.logo.url if wedstrijd.away_team.club.logo else None,
            'away_score': await sync_to_async(Shot.objects.filter(match=wedstrijd, team=wedstrijd.away_team, scored=True).count)(),
            'start_date': formatted_date,
            'start_time': formatted_time,  # Add the time separately
            'length': wedstrijd.part_lenght,
            'finished': wedstrijd.finished,
            'winner': await sync_to_async(wedstrijd.get_winner().__str__)() if wedstrijd.get_winner() else None,
            'get_absolute_url': str(wedstrijd.get_absolute_url())
        })
        
    return wedstrijden_dict