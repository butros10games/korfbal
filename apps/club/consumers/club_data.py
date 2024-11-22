import json
from typing import List, Dict, Any, Optional, TypedDict

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Q

from apps.common.utils import transform_matchdata
from apps.game_tracker.models import MatchData
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team


class TeamJSON(TypedDict):
    id: str
    name: str
    logo: str
    get_absolute_url: str


class ClubDataConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.player: Optional[Player] = None
        self.club: Optional[str] = None

    async def connect(self) -> None:
        self.club = self.scope["url_route"]["kwargs"]["id"]
        await self.accept()

    async def receive(
        self, text_data: Optional[str] = None, bytes_data: Optional[bytes] = None
    ) -> None:
        if text_data is None:
            await self.send(
                text_data=json.dumps({"error": "No data received"})
            )
            return

        try:
            json_data: Dict[str, Any] = json.loads(text_data)
            command: str = json_data["command"]
        except json.JSONDecodeError:
            await self.send(
                text_data=json.dumps({"error": "Invalid JSON"})
            )
            return

        if command == "teams":
            await self.teams_request()
        elif command in {"wedstrijden", "ended_matches"}:
            await self.matches_request(command)
        elif command == "follow":
            await self.follow_request(
                json_data["followed"], json_data["user_id"]
            )

    async def teams_request(self) -> None:
        teams: List[Team] = await sync_to_async(list)(
            Team.objects.filter(club=self.club)
        )
        teams = list(dict.fromkeys(teams))  # Remove duplicates

        teams_json: List[TeamJSON] = [
            {
                "id": str(team.id_uuid),
                "name": await sync_to_async(team.__str__)(),
                "logo": team.club.get_club_logo(),
                "get_absolute_url": str(team.get_absolute_url()),
            }
            for team in teams
        ]

        await self.send(
            text_data=json.dumps({"command": "teams", "teams": teams_json})
        )

    async def matches_request(self, command: str) -> None:
        teams: List[Team] = await sync_to_async(list)(
            Team.objects.filter(club=self.club)
        )
        team_ids: List[str] = [str(team.id_uuid) for team in teams]

        status: List[str]
        order: str
        if command == "wedstrijden":
            status = ["upcoming", "active"]
            order = ""
        else:
            status = ["finished"]
            order = "-"

        wedstrijden_data: List[MatchData] = await self.get_matchs_data(
            team_ids, status, order
        )
        wedstrijden_dict = await transform_matchdata(wedstrijden_data)

        await self.send(
            text_data=json.dumps(
                {"command": "wedstrijden", "wedstrijden": wedstrijden_dict}
            )
        )

    async def get_matchs_data(
        self, team_ids: List[str], status: List[str], order: str
    ) -> List[MatchData]:
        matches: List[Match] = await sync_to_async(list)(
            Match.objects.filter(
                Q(home_team__in=team_ids) | Q(away_team__in=team_ids)
            ).distinct()
        )
        matches_non_dub: List[Match] = list(dict.fromkeys(matches))

        matchs_data: List[MatchData] = await sync_to_async(list)(
            MatchData.objects.prefetch_related(
                "match_link",
                "match_link__home_team",
                "match_link__home_team__club",
                "match_link__away_team",
                "match_link__away_team__club",
            )
            .filter(match_link__in=matches_non_dub, status__in=status)
            .order_by(order + "match_link__start_time")
        )

        return matchs_data

    async def follow_request(self, follow: bool, user_id: str) -> None:
        player: Player = await sync_to_async(Player.objects.get)(user=user_id)

        if follow:
            await sync_to_async(player.club_follow.add)(self.club)
        else:
            await sync_to_async(player.club_follow.remove)(self.club)

        await self.send(
            text_data=json.dumps({"command": "follow", "status": "success"})
        )
