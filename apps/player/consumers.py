import json
import traceback
import locale

from datetime import datetime

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from django.db.models import Q
from apps.game_tracker.models import Shot, GoalType, MatchData
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
        self.player = await Player.objects.prefetch_related('user').aget(id_uuid=player_id)
        self.user = self.player.user
        self.user_profile = await UserProfile.objects.aget(user=self.user)
        await self.accept()
    
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data['command']
            
            if command == "player_stats":
                total_goals_for = 0
                total_goals_against = 0
                
                all_finished_match_data = await self.get_all_finished_matches()

                goal_types = await sync_to_async(list)(GoalType.objects.all())

                player_goal_stats = {}
                scoring_types = []

                for goal_type in goal_types:
                    goals_by_player = await sync_to_async(Shot.objects.filter(
                        match__in=all_finished_match_data, 
                        shot_type=goal_type, 
                        player=self.player,
                        for_team=True,
                        scored=True
                    ).count)()
                    
                    goals_against_player = await sync_to_async(Shot.objects.filter(
                        match__in=all_finished_match_data, 
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
                    'played_matches': await sync_to_async(all_finished_match_data.count)() if all_finished_match_data else 0,
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
                    'email_2fa': self.user_profile.email_2fa
                }))
            
            if command == "settings_update":
                data = json_data['data']
                username = data['username']
                email = data['email']
                first_name = data['first_name']
                last_name = data['last_name']
                email_2fa = data['email_2fa']
                
                self.user.username = username
                self.user.email = email
                self.user.first_name = first_name
                self.user.last_name = last_name
                await self.user.asave()
                
                self.user_profile.email_2fa = email_2fa
                await self.user_profile.asave()
                
                await self.send(text_data=json.dumps({
                    'command': 'settings_updated',
                }))
                
            if command == 'update_profile_picture_url':
                url = json_data['url']
                if url:
                    self.player.profile_picture = url  # Assuming 'url' contains the relative path of the image
                    await self.player.asave()

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
                
            if command == "upcomming_matches" or command == "past_matches":
                matchs_data = await self.get_matchs_data(['upcoming', 'active'] if command == "upcomming_matches" else 'finished')
                
                upcomming_matches_dict = await transfrom_matchdata(matchs_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'matches',
                    'matches': upcomming_matches_dict
                }))
            
        except Exception as e:
            await self.send(text_data=json.dumps({
                'error': str(e),
                'traceback': traceback.format_exc()
            }))
            
    async def get_all_finished_matches(self):
        all_matches_with_player = await sync_to_async(Match.objects.filter)(
            Q(home_team__team_data__players=self.player) |
            Q(away_team__team_data__players=self.player)
        )
                
        return await sync_to_async(list)(MatchData.objects.filter(match_link__in=all_matches_with_player, status='finished'))
    
    async def get_matchs_data(self, status):
        matches = await sync_to_async(list)(Match.objects.filter(
            Q(home_team__team_data__players=self.player) |
            Q(away_team__team_data__players=self.player) |
            Q(home_team__team_data__coach=self.player) |
            Q(away_team__team_data__coach=self.player)
        ).order_by("start_time").distinct())
        
        matches_non_dub = list(dict.fromkeys(matches))
        
        matchs_data = await sync_to_async(list)(MatchData.objects.prefetch_related(
            'match_link', 
            'match_link__home_team', 
            'match_link__home_team__club', 
            'match_link__away_team', 
            'match_link__away_team__club'
        ).filter(match_link__in=matches_non_dub, status__in=status))
        
        return matchs_data
            
async def transfrom_matchdata(matchs_data):
    match_dict = []
            
    for match_data in matchs_data:
        locale.setlocale(locale.LC_TIME, 'nl_NL.utf8')
        start_time_dt = datetime.fromisoformat(match_data.start_time.isoformat())
        
        # Format the date as "za 01 april"
        formatted_date = start_time_dt.strftime("%a %d %b").lower()  # %a for abbreviated day name

        # Extract the time as "14:45"
        formatted_time = start_time_dt.strftime("%H:%M")
        
        home_team = match_data.match_link.home_team
        away_team = match_data.match_link.away_team

        match_dict.append({
            'id_uuid': str(match_data.match_link.id_uuid),
            'home_team': await sync_to_async(home_team.__str__)(),
            'home_team_logo': home_team.club.logo.url if home_team.club.logo else None,
            'home_score': await sync_to_async(Shot.objects.filter(match=match_data, team=home_team, scored=True).count)(),
            'away_team': await sync_to_async(away_team.__str__)(),
            'away_team_logo': away_team.club.logo.url if away_team.club.logo else None,
            'away_score': await sync_to_async(Shot.objects.filter(match=match_data, team=away_team, scored=True).count)(),
            'start_date': formatted_date,
            'start_time': formatted_time,  # Add the time separately
            'length': match_data.part_lenght,
            'finished': match_data.finished,
            'winner': await sync_to_async(match_data.get_winner().__str__)() if match_data.get_winner() else None,
            'get_absolute_url': str(match_data.match_link.get_absolute_url())
        })
        
    return match_dict