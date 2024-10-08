from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.db.models import Q

from apps.team.models import Team
from apps.player.models import Player
from apps.schedule.models import Match

import json
import traceback
import locale

from datetime import datetime

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
                
                # remove dubplicates
                teams = list(dict.fromkeys(teams))
                
                teams_json = [
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
                    'teams': teams_json
                }))
            
            elif command == "wedstrijden":
                teams = await sync_to_async(list)(Team.objects.filter(club=self.club))
                team_ids = [team.id_uuid for team in teams]
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'home_team__club', 'away_team', 'away_team__club').filter(Q(home_team__in=team_ids) | Q(away_team__in=team_ids), finished=False).order_by('start_time'))
                
                wedstrijden_dict = await transfrom_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    'command': 'wedstrijden',
                    'wedstrijden': wedstrijden_dict
                }))
                
            elif command == "ended_matches":
                teams = await sync_to_async(list)(Team.objects.filter(club=self.club))
                team_ids = [team.id_uuid for team in teams]
                wedstrijden_data = await sync_to_async(list)(Match.objects.prefetch_related('home_team', 'home_team__club', 'away_team', 'away_team__club').filter(Q(home_team__in=team_ids) | Q(away_team__in=team_ids), finished=True).order_by('-start_time'))
                
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