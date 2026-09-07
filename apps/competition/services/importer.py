"""Normalize observed Sportlink catalogue endpoints without storing player data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.competition.models import (
    Club,
    Match,
    Pool,
    PoolEntry,
    ResultRevision,
    SyncResource,
    Team,
    TeamGroup,
)
from apps.competition.services.identities import team_group_key
from apps.competition.services.logos import cache_logo, discover_logo
from apps.schedule.models import Season


STANDING_FIELDS = (
    "Position",
    "TotalMatches",
    "Won",
    "Draw",
    "Lost",
    "TotalPoints",
    "PenaltyPoints",
    "GoalsFor",
    "GoalsAgainst",
    "GoalsDifference",
)


def enqueue(season: Season, kind: str, source_id: str = "") -> None:
    """Discover a resource once without resetting its successful checkpoint."""
    SyncResource.objects.get_or_create(
        season=season,
        kind=kind,
        source_id=source_id,
        defaults={"next_sync_at": timezone.now()},
    )


class Importer:
    """Import one response atomically; repeated discovery is idempotent."""

    def __init__(self, season: Season, observed_at: datetime) -> None:
        """Bind each import to an explicit season and observation time."""
        self.season = season
        self.observed_at = observed_at
        self._clubs: dict[str, Club] = {}
        self._teams: dict[str, Team] = {}
        self._pools: dict[str, Pool] = {}

    def club(self, data: dict[str, Any]) -> Club:
        """Upsert club catalogue fields and discover its three collection feeds."""
        source_id = str(data["ClubId"])
        if source_id in self._clubs:
            return self._clubs[source_id]
        club, _ = Club.objects.update_or_create(
            external_id=str(data["ClubId"]),
            defaults={"name": data["ClubName"], "city": data.get("City") or ""},
        )
        for kind in ("club_teams", "club_program", "club_results"):
            enqueue(self.season, kind, club.external_id)
        discover_logo(club, data.get("ClubLogo"), self.season)
        self._clubs[source_id] = club
        return club

    def team(self, data: dict[str, Any]) -> Team:
        """Preserve source team identity, sport and season."""
        source_id = str(data["PublicTeamId"])
        if source_id in self._teams:
            return self._teams[source_id]
        team, _ = Team.objects.update_or_create(
            season=self.season,
            external_id=str(data["PublicTeamId"]),
            defaults={
                "name": data["TeamName"],
                "club": self.club(data["Club"]),
                "sport": data.get("SportId") or "",
            },
        )
        if team.group_id is None:
            team.group, _ = TeamGroup.objects.get_or_create(
                season=self.season,
                club=team.club,
                normalized_name=team_group_key(team.name, team.club.name),
                defaults={"name": team.name},
            )
            team.save(update_fields=("group",))
        enqueue(self.season, "team_pools", team.external_id)
        self._teams[source_id] = team
        return team

    def pool(self, data: dict[str, Any], sport: str = "") -> Pool:
        """Upsert a poule without blank summary fields erasing known metadata."""
        source_id = str(data["PoolId"])
        if source_id in self._pools:
            return self._pools[source_id]
        values = {
            "name": data.get("PoolName"),
            "class_name": data.get("ClassName"),
            "sport": sport,
        }
        pool, _ = Pool.objects.update_or_create(
            season=self.season,
            external_id=str(data["PoolId"]),
            defaults={key: value for key, value in values.items() if value},
        )
        enqueue(self.season, "pool_results", pool.external_id)
        self._pools[source_id] = pool
        return pool

    def match(self, data: dict[str, Any], *, result: bool) -> None:
        """Upsert a fixture and retain score revisions from result feeds only.

        Raises:
            ValueError: Match teams or timestamp are inconsistent.

        """
        starts_at = parse_datetime(data["MatchDateTime"])
        if starts_at is None or timezone.is_naive(starts_at):
            raise ValueError("MatchDateTime must contain a timezone")
        if not self.season.start_date <= starts_at.date() <= self.season.end_date:
            return
        home = self.team(data["HomeTeam"])
        away = self.team(data["AwayTeam"])
        if home == away or home.sport != away.sport:
            raise ValueError("Inconsistent match teams")
        values = {"home_team": home, "away_team": away, "starts_at": starts_at}
        if data.get("Pool"):
            values["pool"] = self.pool(data["Pool"], home.sport)
        match, created = Match.objects.get_or_create(
            season=self.season,
            external_id=str(data["PublicMatchId"]),
            defaults={**values, "status": data["Status"]},
        )
        # Fixture summaries lack scores and must never erase an observed result.
        if not result and match.result_observed_at:
            return
        for key, value in values.items():
            setattr(match, key, value)
        if result:
            self._result(match, data, created=created)
        else:
            match.status = data["Status"]
            match.save()

    def _result(self, match: Match, data: dict[str, Any], *, created: bool) -> None:
        """Apply only newer observations, including score removals/cancellations."""
        if match.result_observed_at and match.result_observed_at > self.observed_at:
            return
        values = {
            "status": data["Status"],
            "home_score": (data.get("HomeResult") or {}).get("Score"),
            "away_score": (data.get("AwayResult") or {}).get("Score"),
            "automatic_result": data.get("AutoResult") is not None,
        }
        changed = created or any(
            getattr(match, key) != value for key, value in values.items()
        )
        for key, value in values.items():
            setattr(match, key, value)
        match.result_observed_at = self.observed_at
        match.results_checked_at = self.observed_at
        match.save()
        if changed:
            ResultRevision.objects.create(
                match=match, observed_at=self.observed_at, **values
            )

    def assignments(self, data: dict[str, Any], source_id: str) -> None:
        """Discover poules and members from a team's published assignments."""
        team = Team.objects.get(season=self.season, external_id=source_id)
        for row in data["TeamPool"]:
            pool = self.pool(row, team.sport)
            PoolEntry.objects.get_or_create(pool=pool, team=team)
            for member in (row.get("PoolAssignment") or {}).get(
                "TeamInPoolAssignment", []
            ):
                PoolEntry.objects.get_or_create(pool=pool, team=self.team(member))

    def standings(self, data: dict[str, Any], source_id: str) -> None:
        """Replace official standing values atomically, without inferring points."""
        pool = Pool.objects.get(season=self.season, external_id=source_id)
        if pool.standings_synced_at and pool.standings_synced_at > self.observed_at:
            return
        for row in data["MatchResult"]:
            self.match(row, result=True)
        rows = (data["PoolStanding"] or {}).get("PoolStandingTeam", [])
        # Compare the complete table once: unchanged polling must not rewrite
        # every membership, while missing rows still lose their old standings.
        standings = {
            self.team(row).pk: {key: row[key] for key in STANDING_FIELDS if key in row}
            for row in rows
        }
        existing = {
            entry.team_id: entry for entry in PoolEntry.objects.filter(pool=pool)
        }
        changed = []
        for team_id in existing.keys() | standings.keys():
            standing = standings.get(team_id, {})
            entry = existing.get(team_id)
            if entry is not None and entry.standing == standing:
                continue
            changed.append(PoolEntry(pool=pool, team_id=team_id, standing=standing))
        PoolEntry.objects.bulk_create(
            changed,
            update_conflicts=True,
            unique_fields=("pool", "team"),
            update_fields=("standing",),
        )
        pool.standings_synced_at = self.observed_at
        pool.results_filtered = data["ResultsFiltered"]
        pool.save(update_fields=("standings_synced_at", "results_filtered"))

    @transaction.atomic
    def apply(self, kind: str, source_id: str, data: dict[str, Any]) -> None:
        """Reject unknown envelopes and roll back malformed collection responses.

        Raises:
            ValueError: The provider response or resource kind is invalid.

        """
        self._clubs.clear()
        self._teams.clear()
        self._pools.clear()
        if data.get("Error"):
            raise ValueError("Sportlink returned an application error")
        collections = {
            "clubs": ("Club", self.club),
            "club_teams": ("ClubTeam", self.team),
        }
        if kind in collections:
            field, import_row = collections[kind]
            for row in data[field]:
                import_row(row)
        elif kind == "club_logo":
            cache_logo(source_id, data)
        elif kind == "team_pools":
            self.assignments(data, source_id)
        elif kind == "pool_results":
            self.standings(data, source_id)
        elif kind in {"club_program", "club_results"}:
            self.match_collection(kind, data)
        else:
            raise ValueError("Unsupported competition resource")

    def match_collection(self, kind: str, data: dict[str, Any]) -> None:
        """Import the provider's fixture or result envelope."""
        is_result = kind == "club_results"
        key = "MatchResult" if is_result else "ProgramItemMatchClub"
        for row in data[key]:
            self.match(row if is_result else row["Match"], result=is_result)
