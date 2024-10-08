import json
import traceback
import locale

from datetime import datetime

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from django.db.models import Q
from apps.game_tracker.models import Shot, GoalType
from apps.schedule.models import Match
from apps.player.models import Player
from apps.team.models import Team
from authentication.models import UserProfile

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
                    goals_by_player = await sync_to_async(Shot.objects.filter(
                        match__in=all_matches_with_player, 
                        shot_type=goal_type, 
                        player=self.player,
                        for_team=True,
                        scored=True
                    ).count)()
                    
                    goals_against_player = await sync_to_async(Shot.objects.filter(
                        match__in=all_matches_with_player, 
                        shot_type=goal_type, 
                        player=self.player,
                        for_team=False,
                        scored=True
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
                
                # remove dubplicates
                teams = list(dict.fromkeys(teams))
                
                teams_dict = [
                    {
                        'id': str(team.id_uuid),
                        'name': await sync_to_async(team.__str__)(),
                        'logo': team.club.logo.url if team.club.logo else None,
                        'get_absolute_url': str(team.get_absolute_url())
                    }
                    for team in teams
                ]
                
                await self.send(text_data=json.dumps({
                    'command': 'teams',
                    'teams': teams_dict
                }))
                
            if command == "upcomming_matches":
                upcomming_matches = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'home_team__club', 'away_team', 'away_team__club').filter(
                    Q(home_team__team_data__players=self.player, finished=False) |
                    Q(away_team__team_data__players=self.player, finished=False) |
                    Q(home_team__team_data__coach=self.player, finished=False) |
                    Q(away_team__team_data__coach=self.player, finished=False)
                ).order_by("start_time").distinct())
                
                # remove dubplicates
                upcomming_matches = list(dict.fromkeys(upcomming_matches))
                
                upcomming_matches_dict = await transfrom_matchdata(upcomming_matches)
                
                await self.send(text_data=json.dumps({
                    'command': 'upcomming-matches',
                    'matches': upcomming_matches_dict
                }))
                
            if command == "past_matches":
                upcomming_matches = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'home_team__club', 'away_team', 'away_team__club').filter(
                    Q(home_team__team_data__players=self.player, finished=True) |
                    Q(away_team__team_data__players=self.player, finished=True) |
                    Q(home_team__team_data__coach=self.player, finished=True) |
                    Q(away_team__team_data__coach=self.player, finished=True)
                ).order_by("start_time").distinct())
                
                # remove dubplicates
                upcomming_matches = list(dict.fromkeys(upcomming_matches))
                
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