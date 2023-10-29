from channels.generic.websocket import AsyncWebsocketConsumer, WebsocketConsumer
from asgiref.sync import sync_to_async
from django.db.models import Q

from .models import Team, Match, Goal, GoalType, Season, TeamData, Player, PlayerChange, Pause, PlayerGroup, GroupTypes, Shot, MatchPart
from authentication.models import UserProfile
from django.core.files.base import ContentFile

import json
import traceback
import locale
from datetime import datetime
import base64

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
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'away_team').filter(Q(home_team=self.team) | Q(away_team=self.team), finished=False).order_by('start_time'))
                
                wedstrijden_dict = await transfrom_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'wedstrijden',
                    'wedstrijden': wedstrijden_dict
                }))
                
            elif command == "ended_matches":
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'away_team').filter(Q(home_team=self.team) | Q(away_team=self.team), finished=True).order_by('-start_time'))
                
                wedstrijden_dict = await transfrom_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'wedstrijden',
                    'wedstrijden': wedstrijden_dict
                }))
                
            elif command == "team_stats":
                all_matches = await sync_to_async(Match.objects.filter)(Q(home_team=self.team, finished=True) | Q(away_team=self.team, finished=True))
                all_matches_list = await sync_to_async(list)(all_matches.prefetch_related('home_team', 'away_team'))
                
                goal_types = await sync_to_async(list)(GoalType.objects.all())

                goal_stats = {}
                scoring_types = []
                played_matches = await sync_to_async(all_matches.count)()
                total_goals_for = 0
                total_goals_against = 0
                
                for goal_type in goal_types:
                    goals_for = await sync_to_async(Goal.objects.filter(match__in=all_matches, goal_type=goal_type, for_team=True).count)()
                    goals_against = await sync_to_async(Goal.objects.filter(match__in=all_matches, goal_type=goal_type, for_team=False).count)()
            
                    goal_stats[goal_type.name] = {
                        "goals_for": goals_for,
                        "goals_against": goals_against
                    }
                    
                    scoring_types.append(goal_type.name)
                    
                for match in all_matches_list:
                    total_goals_for += match.home_score if match.home_team == self.team else match.away_score
                    total_goals_against += match.away_score if match.home_team == self.team else match.home_score
                
                await self.send(text_data=json.dumps({
                    'command': 'goal_stats',
                    'goal_stats': goal_stats,
                    'scoring_types': scoring_types,
                    'played_matches': played_matches,
                    'total_goals_for': total_goals_for,
                    'total_goals_against': total_goals_against,
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
                    # No specific season provided, so retrieve players from all seasons
                    all_team_data_instances = await sync_to_async(list)(TeamData.objects.prefetch_related('players').filter(team=self.team))

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
                    'spelers':players_in_team_season_dict,
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
            
class profile_data(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.player = None
        self.user_profile = None
        
    async def connect(self):
        player_id = self.scope['url_route']['kwargs']['id']
        self.player = await sync_to_async(Player.objects.prefetch_related('user').get)(id_uuid=player_id)
        self.user = self.player.user
        self.user_profile = await sync_to_async(UserProfile.objects.get)(user=self.user)
        await self.accept()
        
    async def disconnect(self, close_code):
        pass
    
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data['command']
            
            if command == "player_stats":
                total_goals_for = 0
                total_goals_against = 0
                
                all_matches_with_player = await sync_to_async(Match.objects.filter)(
                    Q(home_team__team_data__players=self.player, finished=True) |
                    Q(away_team__team_data__players=self.player, finished=True)
                )

                goal_types = await sync_to_async(list)(GoalType.objects.all())

                player_goal_stats = {}
                scoring_types = []

                for goal_type in goal_types:
                    goals_by_player = await sync_to_async(Goal.objects.filter(
                        match__in=all_matches_with_player, 
                        goal_type=goal_type, 
                        player=self.player,
                        for_team=True
                    ).count)()
                    
                    goals_against_player = await sync_to_async(Goal.objects.filter(
                        match__in=all_matches_with_player, 
                        goal_type=goal_type, 
                        player=self.player,
                        for_team=False
                    ).count)()

                    player_goal_stats[goal_type.name] = {
                        "goals_by_player": goals_by_player,
                        "goals_against_player": goals_against_player
                    }
                    
                    total_goals_for += goals_by_player
                    total_goals_against += goals_against_player

                    scoring_types.append(goal_type.name)

                await self.send(text_data=json.dumps({
                    'command': 'player_goal_stats',
                    'player_goal_stats': player_goal_stats,
                    'scoring_types': scoring_types,
                    'played_matches': await sync_to_async(all_matches_with_player.count)(),
                    'total_goals_for': total_goals_for,
                    'total_goals_against': total_goals_against,
                }))
                
            if command == "settings_request":
                await self.send(text_data=json.dumps({
                    'command': 'settings_request',
                    'username': self.user.username,
                    'email': self.user.email,
                    'first_name': self.user.first_name,
                    'last_name': self.user.last_name,
                    'is_2fa_enabled': self.user_profile.is_2fa_enabled
                }))
            
            if command == "settings_update":
                data = json_data['data']
                username = data['username']
                email = data['email']
                first_name = data['first_name']
                last_name = data['last_name']
                is_2fa_enabled = data['is_2fa_enabled']
                
                self.user.username = username
                self.user.email = email
                self.user.first_name = first_name
                self.user.last_name = last_name
                await sync_to_async(self.user.save)()
                
                self.user_profile.is_2fa_enabled = is_2fa_enabled
                await sync_to_async(self.user_profile.save)()
                
                await self.send(text_data=json.dumps({
                    'command': 'settings_updated',
                }))
                
            if command == 'update_profile_picture_url':
                url = json_data['url']
                if url:
                    self.player.profile_picture = url  # Assuming 'url' contains the relative path of the image
                    await sync_to_async(self.player.save)()

                    # Send a response back to the client if needed
                    await self.send(text_data=json.dumps({
                        'command': 'profile_picture_updated',
                        'status': 'success'
                    }))
                    
            if command == 'teams':
                teams = await sync_to_async(list)(Team.objects.filter(team_data__players=self.player))
                
                teams_dict = [
                    {
                        'id': str(team.id_uuid),
                        'name': team.name,
                        'get_absolute_url': str(team.get_absolute_url())
                    }
                    for team in teams
                ]
                
                await self.send(text_data=json.dumps({
                    'command': 'teams',
                    'teams': teams_dict
                }))
                
            if command == "upcomming_matches":
                upcomming_matches = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'away_team').filter(
                    Q(home_team__team_data__players=self.player, finished=False) |
                    Q(away_team__team_data__players=self.player, finished=False)
                ))
                
                upcomming_matches_dict = await transfrom_matchdata(upcomming_matches)
                
                await self.send(text_data=json.dumps({
                    'command': 'upcomming-matches',
                    'matches': upcomming_matches_dict
                }))
                
            if command == "past_matches":
                upcomming_matches = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'away_team').filter(
                    Q(home_team__team_data__players=self.player, finished=True) |
                    Q(away_team__team_data__players=self.player, finished=True)
                ))
                
                upcomming_matches_dict = await transfrom_matchdata(upcomming_matches)
                
                await self.send(text_data=json.dumps({
                    'command': 'past-matches',
                    'matches': upcomming_matches_dict
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
            'home_team': wedstrijd.home_team.name,
            'away_team': wedstrijd.away_team.name,
            'home_score': wedstrijd.home_score,
            'away_score': wedstrijd.away_score,
            'start_date': formatted_date,
            'start_time': formatted_time,  # Add the time separately
            'length': wedstrijd.length,
            'finished': wedstrijd.finished,
            'winner': wedstrijd.get_winner().name if wedstrijd.get_winner() else None,
            'get_absolute_url': str(wedstrijd.get_absolute_url())
        })
        
    return wedstrijden_dict

class club_data(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.player = None
        self.user_profile = None
        self.club = None
        
    async def connect(self):
        self.club = self.scope['url_route']['kwargs']['id']
        await self.accept()
        
    async def disconnect(self, close_code):
        pass
    
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data['command']
            
            if command == "teams":
                teams = await sync_to_async(list)(Team.objects.filter(club=self.club))
                
                teams_json = [
                    {
                        'id': str(team.id_uuid),
                        'name': team.name,
                        'get_absolute_url': str(team.get_absolute_url())
                    }
                    for team in teams
                ]
                
                await self.send(text_data=json.dumps({
                    'command': 'teams',
                    'teams': teams_json
                }))
            
            elif command == "wedstrijden":
                teams = await sync_to_async(list)(Team.objects.filter(club=self.club))
                team_ids = [team.id_uuid for team in teams]
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'away_team').filter(Q(home_team__in=team_ids) | Q(away_team__in=team_ids), finished=False).order_by('start_time'))
                
                wedstrijden_dict = await transfrom_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'wedstrijden',
                    'wedstrijden': wedstrijden_dict
                }))
                
            elif command == "ended_matches":
                teams = await sync_to_async(list)(Team.objects.filter(club=self.club))
                team_ids = [team.id_uuid for team in teams]
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'away_team').filter(Q(home_team__in=team_ids) | Q(away_team__in=team_ids), finished=True).order_by('-start_time'))
                
                wedstrijden_dict = await transfrom_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'wedstrijden',
                    'wedstrijden': wedstrijden_dict
                }))
            
            elif command == "follow":
                follow = json_data['followed']
                user_id = json_data['user_id']
                
                player = await sync_to_async(Player.objects.get)(user=user_id)
                
                if follow:
                    await sync_to_async(player.club_follow.add)(self.club)
                    
                else:
                    await sync_to_async(player.club_follow.remove)(self.club)
                
                await self.send(text_data=json.dumps({
                    'command': 'follow',
                    'status': 'success'
                }))
            
        except Exception as e:
            await self.send(text_data=json.dumps({
                'error': str(e),
                'traceback': traceback.format_exc()
            }))
            
class match_data(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.match = None
        
    async def connect(self):
        match_id = self.scope['url_route']['kwargs']['id']
        self.match = await sync_to_async(Match.objects.prefetch_related('home_team','away_team').get)(id_uuid=match_id)
        
        self.channel_group_name = 'match_%s' % self.match.id_uuid
        await self.channel_layer.group_add(self.channel_group_name, self.channel_name)
        
        await self.accept()
        
    async def disconnect(self, close_code):
        pass
    
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data['command']
            
            if command == "match_events":
                try:
                    part = await sync_to_async(MatchPart.objects.get)(match=self.match, active=True)
                except MatchPart.DoesNotExist:
                    part = None
                    
                events_dict = []
                
                # check if there is a part active or the match is finished
                if part != None or self.match.finished:
                    goals = await sync_to_async(list)(Goal.objects.prefetch_related('player__user', 'goal_type').filter(match=self.match).order_by('time'))
                    player_change = await sync_to_async(list)(PlayerChange.objects.prefetch_related('player_in', 'player_out__user').filter(player_group__match=self.match).order_by('time'))
                    time_outs = await sync_to_async(list)(Pause.objects.filter(match=self.match).order_by('start_time'))
                    
                    # add all the events to a list and order them on time
                    events = []
                    events.extend(goals)
                    events.extend(player_change)
                    events.extend(time_outs)
                    events.sort(key=lambda x: x.time)
                    
                    for event in events:
                        if isinstance(event, Goal):
                            time_in_minutes = (event.time - part.start_time).total_seconds() / 60
                            
                            events_dict.append({
                                'type': 'goal',
                                'time': time_in_minutes,
                                'player': event.player.user.username,
                                'goal_type': event.goal_type.name,
                                'for_team': event.for_team
                            })
                        elif isinstance(event, PlayerChange):
                            time_in_minutes = (event.time - part.start_time).total_seconds() / 60
                            
                            events_dict.append({
                                'type': 'player_change',
                                'time': time_in_minutes,
                                'player_in': event.player_in.user.username,
                                'player_out': event.player_out.user.username,
                                'player_group': event.player_group.id_uuid
                            })
                        elif isinstance(event, Pause):
                            # calculate the time in minutes sinds the real_start_time of the match and the start_time of the pause
                            time_in_minutes = (event.start_time - part.start_time).total_seconds() / 60
                            
                            events_dict.append({
                                'type': 'pause',
                                'time': time_in_minutes,
                                'length': event.length
                            })
                    
                ## Check if player is in the home or away team
                user_id = json_data['user_id']
                player = await sync_to_async(Player.objects.get)(user=user_id)
                
                players_home = await sync_to_async(list)(TeamData.objects.prefetch_related('players').filter(team=self.match.home_team).values_list('players', flat=True))
                players_away = await sync_to_async(list)(TeamData.objects.prefetch_related('players').filter(team=self.match.away_team).values_list('players', flat=True))
                
                players_list = []
                
                players_list.extend(players_home)
                players_list.extend(players_away)
                
                access = False
                if player.id_uuid in players_list:
                    access = True
                
                await self.send(text_data=json.dumps({
                    'command': 'events',
                    'events': events_dict,
                    'access': access,
                    'finished': self.match.finished
                }))
            
            elif command == "home_team":
                user_id = json_data['user_id']
                team = self.match.home_team
                
                player_groups_array = await self.makePlayerGroupList(team)
                
                players_json = await self.makePlayerList(team)
                    
                is_coach = await self.checkIfCoach(user_id, team)
                
                await self.send(text_data=json.dumps({
                    'command': 'playerGroups',
                    'playerGroups': player_groups_array,
                    'players': players_json,
                    'is_coach': is_coach
                }))
                
            elif command == "away_team":
                user_id = json_data['user_id']
                team = self.match.away_team
                
                player_groups_array = await self.makePlayerGroupList(team)
                
                players_json = await self.makePlayerList(team)
                
                is_coach = await self.checkIfCoach(user_id, team)
                
                await self.send(text_data=json.dumps({
                    'command': 'playerGroups',
                    'playerGroups': player_groups_array,
                    'players': players_json,
                    'is_coach': is_coach
                }))
            
            elif command == "follow":
                follow = json_data['followed']
                user_id = json_data['user_id']
                
                player = await sync_to_async(TeamData.objects.get)(user=user_id)
                
                if follow:
                    await sync_to_async(player.club_follow.add)(self.club)
                    
                else:
                    await sync_to_async(player.club_follow.remove)(self.club)
                
                await self.send(text_data=json.dumps({
                    'command': 'follow',
                    'status': 'success'
                }))
                
            elif command == "savePlayerGroups":
                player_groups = json_data['playerGroups']
                
                for player_group in player_groups:
                    group = await sync_to_async(PlayerGroup.objects.get)(id_uuid=player_group['id'])
                    await sync_to_async(group.players.clear)()
                    
                    for player in player_group['players']:
                        if player == 'NaN':
                            continue
                        player_obj = await sync_to_async(Player.objects.get)(id_uuid=player)
                        await sync_to_async(group.players.add)(player_obj)
                        
                    await sync_to_async(group.save)()
                
                await self.send(text_data=json.dumps({
                    'command': 'savePlayerGroups',
                    'status': 'success'
                }))
            
        except Exception as e:
            await self.send(text_data=json.dumps({
                'error': str(e),
                'traceback': traceback.format_exc()
            }))
            
    async def makePlayerGroupList(self, team):
        try:
            player_groups = await sync_to_async(list)(PlayerGroup.objects.prefetch_related('players', 'players__user', 'starting_type', 'current_type').filter(match=self.match, team=team).order_by('starting_type'))
            
            # When there is no connected player group create the player groups
            if player_groups == []:
                group_types = await sync_to_async(list)(GroupTypes.objects.all())
                
                for group_type in group_types:
                    await sync_to_async(PlayerGroup.objects.create)(match=self.match, team=team, starting_type=group_type, current_type=group_type)
                    
                player_groups = await sync_to_async(list)(PlayerGroup.objects.prefetch_related('players', 'players__user', 'starting_type', 'current_type').filter(match=self.match, team=team).order_by('starting_type'))
            
            # make it a json parsable string
            player_groups_array = [
                {
                    'id': str(player_group.id_uuid),
                    'players': [
                        {
                            'id': str(player.id_uuid),
                            'name': player.user.username,
                        }
                        for player in player_group.players.all()
                    ],
                    'starting_type': player_group.starting_type.name,
                    'current_type': player_group.current_type.name
                }
                for player_group in player_groups
            ]
            
            return player_groups_array
        
        except Exception as e:
            await self.send(text_data=json.dumps({
                'error': str(e),
                'traceback': traceback.format_exc(),
                'player_groups': player_groups
            }))
            
    async def makePlayerList(self, team):
        players_json = []
        players = await sync_to_async(list)(TeamData.objects.prefetch_related('players').filter(team=team).values_list('players', flat=True))
        
        for player in players:
            try:
                player_json = await sync_to_async(Player.objects.prefetch_related('user').get)(id_uuid=player)
                players_json.append({
                    'id': str(player_json.id_uuid),
                    'name': player_json.user.username,
                    'profile_picture': player_json.profile_picture.url if player_json.profile_picture else None,
                    'get_absolute_url': str(player_json.get_absolute_url())
                })
            except Player.DoesNotExist:
                pass
                
        return players_json
    
    async def checkIfCoach(self, user_id, team):
        if user_id != 'None':
            player = await sync_to_async(Player.objects.get)(user=user_id)
            
            try:
                team_coach = await sync_to_async(TeamData.objects.get)(team=team, coach=player)
                return True
            except TeamData.DoesNotExist:
                return False
            
        return False
    
    async def send_data(self, event):
        data = event['data']
        await self.send(text_data=json.dumps(data))
    
class match_tracker(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.match = None
        
    async def connect(self):
        match_id = self.scope['url_route']['kwargs']['id']
        self.match = await sync_to_async(Match.objects.prefetch_related('home_team','away_team').get)(id_uuid=match_id)
        self.team = await sync_to_async(Team.objects.get)(id_uuid=self.scope['url_route']['kwargs']['team_id'])
        
        self.channel_group_name = 'match_%s' % self.match.id_uuid
        await self.channel_layer.group_add(self.channel_group_name, self.channel_name)
        
        await self.accept()
        
    async def disconnect(self, close_code):
        pass
    
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data['command']
            
            if command == "playerGroups":
                team = self.team
                
                player_groups_array = await self.makePlayerGroupList(team)
                
                players_json = await self.makePlayerList(team)
                
                await self.send(text_data=json.dumps({
                    'command': 'playerGroups',
                    'playerGroups': player_groups_array,
                    'players': players_json
                }))
                
            elif command == "shot_reg":
                await sync_to_async(Shot.objects.create)(player=await sync_to_async(Player.objects.get)(id_uuid=json_data['player_id']), match=self.match, time = json_data['time'], for_team=json_data['for_team'])
                
                await self.channel_layer.group_send(self.channel_group_name, {
                    'type': 'send_data',
                    'data': {
                        'command': 'player_shot_change',
                        'player_id': json_data['player_id'],
                        'shots': await sync_to_async(Shot.objects.filter(player__id_uuid=json_data['player_id'], match=self.match).count)()
                    }
                })
                
            elif command == "get_goal_types":
                goal_type_list = await sync_to_async(list)(GoalType.objects.all())
                
                goal_type_list = [
                    {
                        'id': str(goal_type.id_uuid),
                        'name': goal_type.name
                    }
                    for goal_type in goal_type_list
                ]
                
                await self.send(text_data=json.dumps({
                    'command': 'goal_types',
                    'goal_types': goal_type_list
                }))
                
            elif command == "goal_reg":
                await sync_to_async(Goal.objects.create)(player=await sync_to_async(Player.objects.get)(id_uuid=json_data['player_id']), match=self.match, time = json_data['time'], goal_type=await sync_to_async(GoalType.objects.get)(id_uuid=json_data['goal_type']), for_team=json_data['for_team'])
                
                player = await sync_to_async(Player.objects.prefetch_related('user').get)(id_uuid=json_data['player_id'])
                
                await self.channel_layer.group_send(self.channel_group_name, {
                    'type': 'send_data',
                    'data': {
                        'command': 'team_goal_change',
                        'player_name': player.user.username,
                        'goal_type': await sync_to_async(GoalType.objects.get)(id_uuid=json_data['goal_type']).name,
                        'goals_for': await sync_to_async(Goal.objects.filter(player__id_uuid=json_data['player_id'], match=self.match, for_team=True).count)(),
                        'goals_against': await sync_to_async(Goal.objects.filter(player__id_uuid=json_data['player_id'], match=self.match, for_team=False).count)()
                    }
                })
                
            elif command == "start/pause":
                try:
                    part = await sync_to_async(MatchPart.objects.get)(match=self.match, active=True)
                    
                    # check if there is a pause active and if not create a pause
                    try:
                        pause = await sync_to_async(Pause.objects.get)(match=self.match, active=True)
                        pause.active = False
                        pause.end_time = datetime.now()
                        await sync_to_async(pause.save)()
                        
                        await self.channel_layer.group_send(self.channel_group_name, {
                            'type': 'send_data',
                            'data': {
                                'command': 'pause',
                                'pause': False
                            }
                        })
                        
                    except Pause.DoesNotExist:
                        pause = await sync_to_async(Pause.objects.create)(match=self.match, active=False, start_time=datetime.now())
                        
                        await self.channel_layer.group_send(self.channel_group_name, {
                            'type': 'send_data',
                            'data': {
                                'command': 'pause',
                                'pause': False
                            }
                        })
                
                except MatchPart.DoesNotExist:
                    part = await sync_to_async(MatchPart.objects.create)(match=self.match, active=True, start_time=datetime.now())
                    
                    await self.channel_layer.group_send(self.channel_group_name, {
                        'type': 'send_data',
                        'data': {
                            'command': 'start',
                            'time': 0
                        }
                    })

        except Exception as e:
                await self.send(text_data=json.dumps({
                    'error': str(e),
                    'traceback': traceback.format_exc()
                }))
                
    async def create_player_groups(self, team):
        group_types = await sync_to_async(list)(GroupTypes.objects.all())
        for group_type in group_types:
            await sync_to_async(PlayerGroup.objects.create)(match=self.match, team=team, starting_type=group_type, current_type=group_type)

    async def get_player_groups(self, team):
        return await sync_to_async(list)(PlayerGroup.objects.prefetch_related('players', 'players__user', 'starting_type', 'current_type').filter(match=self.match, team=team).order_by('starting_type'))

    async def make_player_group_json(self, player_groups):
        async def process_player(player):
            return {
                'id': str(player.id_uuid),
                'name': player.user.username,
                'shots': await sync_to_async(Shot.objects.filter(player=player, match=self.match).count)()
            }

        async def process_player_group(player_group):
            return {
                'id': str(player_group.id_uuid),
                'players': [await process_player(player) for player in player_group.players.all()],
                'starting_type': player_group.starting_type.name,
                'current_type': player_group.current_type.name
            }

        return [await process_player_group(player_group) for player_group in player_groups]

    async def makePlayerGroupList(self, team):
        player_groups = await self.get_player_groups(team)
        if not player_groups:
            await self.create_player_groups(team)
            player_groups = await self.get_player_groups(team)
        return await self.make_player_group_json(player_groups)
            
    async def makePlayerList(self, team):
        players_json = []
        
        # Get all player groups for the team
        player_groups = await self.get_player_groups(team)
        
        # Get all players that are already in a group
        grouped_players = [player for group in player_groups for player in group.players.all()]
        
        # Get all players for the team, excluding those that are already in a group
        players = await sync_to_async(list)(TeamData.objects.prefetch_related('players').filter(team=team).exclude(players__in=grouped_players).values_list('players', flat=True))
        
        for player in players:
            try:
                player_json = await sync_to_async(Player.objects.prefetch_related('user').get)(id_uuid=player)
                players_json.append({
                    'id': str(player_json.id_uuid),
                    'name': player_json.user.username,
                    'profile_picture': player_json.profile_picture.url if player_json.profile_picture else None,
                    'get_absolute_url': str(player_json.get_absolute_url())
                })
            except Player.DoesNotExist:
                pass
                    
        return players_json
    
    async def send_data(self, event):
        data = event['data']
        await self.send(text_data=json.dumps(data))
