from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.db.models import Q

from apps.team.models import Team
from apps.player.models import Player
from apps.schedule.models import Match
from apps.game_tracker.models import MatchData

from apps.common.utils import transform_matchdata

import json
import traceback


class ClubDataConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.player = None
        self.user_profile = None
        self.club = None
        
    async def connect(self):
        self.club = self.scope["url_route"]["kwargs"]["id"]
        await self.accept()
    
    async def receive(self, text_data):
        try:
            json_data = json.loads(text_data)
            command = json_data["command"]
            
            if command == "teams":
                teams = await sync_to_async(list)(Team.objects.filter(club=self.club))
                
                # remove dubplicates
                teams = list(dict.fromkeys(teams))
                
                teams_json = [
                    {
                        "id": str(team.id_uuid),
                        "name": await sync_to_async(team.__str__)(),
                        "logo": team.club.get_club_logo(),
                        "get_absolute_url": str(team.get_absolute_url())
                    }
                    for team in teams
                ]
                
                await self.send(text_data=json.dumps({
                    "command": "teams",
                    "teams": teams_json
                }))
            
            elif command == "wedstrijden" or command == "ended_matches":
                teams = await sync_to_async(list)(Team.objects.filter(club=self.club))
                team_ids = [team.id_uuid for team in teams]
                
                wedstrijden_data = await self.get_matchs_data(
                    team_ids, 
                    ["upcoming", "active"] if command == "wedstrijden" else ["finished"],
                    "" if command == "wedstrijden" else "-"
                )
                
                wedstrijden_dict = await transform_matchdata(wedstrijden_data)
                
                await self.send(text_data=json.dumps({
                    "command": "wedstrijden",
                    "wedstrijden": wedstrijden_dict
                }))
            
            elif command == "follow":
                follow = json_data["followed"]
                user_id = json_data["user_id"]
                
                player = await sync_to_async(Player.objects.get)(user=user_id)
                
                if follow:
                    await sync_to_async(player.club_follow.add)(self.club)
                    
                else:
                    await sync_to_async(player.club_follow.remove)(self.club)
                
                await self.send(text_data=json.dumps({
                    "command": "follow",
                    "status": "success"
                }))
            
        except Exception as e:
            await self.send(text_data=json.dumps({
                "error": str(e),
                "traceback": traceback.format_exc()
            }))
            
    async def get_matchs_data(self, team_ids, status, order):
        matches = await sync_to_async(list)(Match.objects.filter(
            Q(home_team__in=team_ids) | 
            Q(away_team__in=team_ids)
        ).distinct())
        
        matches_non_dub = list(dict.fromkeys(matches))
        
        matchs_data = await sync_to_async(list)(MatchData.objects.prefetch_related(
            "match_link", 
            "match_link__home_team", 
            "match_link__home_team__club", 
            "match_link__away_team", 
            "match_link__away_team__club"
        ).filter(match_link__in=matches_non_dub, status__in=status).order_by(order + "match_link__start_time"))
        
        return matchs_data
