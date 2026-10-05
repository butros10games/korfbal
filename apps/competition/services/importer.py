"""Normalize competition collections and explicitly enabled visible rosters."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
import logging
from typing import Any

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.competition.domain.source_context import context_value, merge_source_context
from apps.competition.domain.standings_provenance import content_digest
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
from apps.competition.services.classification import map_pool
from apps.competition.services.club_details import (
    catalogue_values,
    import_club_contact,
    import_club_details,
    import_club_sports,
    update_club_metadata,
)
from apps.competition.services.computed_standings import refresh_generated_standings
from apps.competition.services.cups import import_observed_cup_fixture
from apps.competition.services.history_integrity import (
    STANDING_FIELDS,
    compare_match,
    match_identity,
    merge_duplicate_match,
    unique_standings,
)
from apps.competition.services.identities import team_group_key
from apps.competition.services.lineups import import_lineup
from apps.competition.services.logos import cache_logo, discover_logo
from apps.competition.services.match_details import (
    import_facility,
    import_rules,
    invalidate_metadata,
    invalidate_pool_metadata,
    metadata_context,
    queue_missing_details,
)
from apps.competition.services.match_timing import (
    import_playing_time,
    import_timing_details,
)
from apps.competition.services.player_photos import cache_photo
from apps.competition.services.rosters import import_roster
from apps.competition.services.seasons import configure_seasons
from apps.competition.services.standings import refresh_official_digests
from apps.schedule.models import Season


logger = logging.getLogger(__name__)


def standing_inputs(match: Match | None) -> tuple[object, ...] | None:
    """Capture only fixture inputs used by generated standings."""
    if match is None:
        return None
    return tuple(
        getattr(match, field)
        for field in (
            "pool_id",
            "home_team_id",
            "away_team_id",
            "starts_at",
            "status",
            "home_score",
            "away_score",
            "automatic_result",
        )
    )


def assign_changed(instance: models.Model, values: dict[str, Any]) -> list[str]:
    """Assign changed snapshot fields without fetching foreign-key objects."""
    changed = []
    for key, value in values.items():
        if getattr(instance, key) != value:
            setattr(instance, key, value)
            changed.append(key)
    return changed


def save_changed(instance: models.Model, values: dict[str, Any]) -> None:
    """Keep stable catalogue rows read-only and preserve model saves for changes."""
    if fields := assign_changed(instance, values):
        instance.save(update_fields=fields)


def enqueue(season: Season, kind: str, source_id: str = "") -> None:
    """Discover a resource once without resetting its successful checkpoint."""
    SyncResource.objects.get_or_create(
        season=season,
        kind=kind,
        source_id=source_id,
        defaults={"next_sync_at": timezone.now()},
    )


def club_with_name(team: dict[str, Any]) -> dict[str, Any]:
    """Name an unnamed club from its team: the team name minus the team code.

    Old poules can list a dissolved club without a name; its teams still carry
    the club name before their code ("Keizer Karel 2", code "2"). Without a
    matching code the name stays empty and publication reports the conflict.
    Such clubs are marked dissolved.
    """
    club = team["Club"]
    if (club.get("ClubName") or "").strip():
        return club
    name, code = team.get("TeamName") or "", (team.get("TeamCode") or "").strip()
    if code and name.endswith(" " + code):
        return {**club, "ClubName": name.removesuffix(code).strip(), "Dissolved": True}
    return {**club, "Dissolved": True}


# Responses bound to one provider identity, independent of the season.
SOURCE_IMPORTS: dict[str, Callable[[str, dict[str, Any]], None]] = {
    "player_photo": cache_photo,
    "club_logo": cache_logo,
}


def _self_fixture(data: dict[str, Any]) -> bool:
    """Reject provider self-fixtures before identity writes or coverage tracking."""
    same_team = str(data["HomeTeam"]["PublicTeamId"]) == str(
        data["AwayTeam"]["PublicTeamId"]
    )
    if same_team:
        logger.warning("Skipping provider fixture with identical home/away teams")
    return same_team


def lock_fixture_pools(scopes: list[Season], rows: list[dict[str, Any]]) -> None:
    """Lock the fixtures' current and incoming poules before any fixture itself.

    Publication locks poules (resolving their periods) before updating the
    fixtures in them. Importers take every poule a batch touches first, in key
    order, so an import and a parallel publication pass cannot deadlock.
    """
    identifiers = {
        str(row["PublicMatchId"]) for row in rows if row.get("PublicMatchId")
    }
    incoming = {
        str(pool["PoolId"])
        for row in rows
        if (pool := row.get("Pool") or {}).get("PoolId")
    }
    if not identifiers and not incoming:
        return
    current = Match.objects.filter(
        season__in=scopes, external_id__in=identifiers
    ).exclude(pool_id=None)
    selected = models.Q(pk__in=current.values("pool_id")) | models.Q(
        season__in=scopes, external_id__in=incoming
    )
    pools = Pool.objects.filter(selected).order_by("pk")
    list(pools.select_for_update(no_key=True).values_list("pk", flat=True))


class Importer:
    """Import one response atomically; repeated discovery is idempotent."""

    def __init__(
        self,
        season: Season,
        observed_at: datetime,
        *,
        discover: bool = True,
        window: tuple[date, date] | None = None,
        expected_metadata_context: str | None = None,
    ) -> None:
        """Bind each import to an explicit season and observation time.

        ``window`` widens the accepted fixture dates beyond the season's own,
        for poules whose period was decided from all of their fixtures.
        """
        self.discover = discover
        self.expected_metadata_context = expected_metadata_context
        self.season = season
        self.window = window or (season.start_date, season.end_date)
        self.observed_at = observed_at
        self._source_kind = "summary" if discover else "historical"
        self.observed_match_ids: set[int] = set()
        self._detail_match_ids: set[int] = set()
        self._seasons_configured = False
        self._clubs: dict[str, Club] = {}
        self._teams: dict[str, Team] = {}
        self._pools: dict[str, Pool] = {}
        self._matches: dict[tuple[str, bool], dict[str, Any]] = {}
        self._entries: set[tuple[int, int]] = set()
        self._touched_pool_ids: set[int] = set()
        self._generated_pool_ids: set[int] = set()
        self._membership_pool_ids: set[int] = set()

    def club(self, data: dict[str, Any]) -> Club:
        """Enrich repeated club observations without replacing newer metadata."""
        source_id = str(data["ClubId"])
        values = catalogue_values(data)
        cached = self._clubs.get(source_id)
        reference = data.get("ClubLogo")
        same_logo = not isinstance(reference, dict) or (
            cached is not None
            and (cached.logo_bucket, cached.logo_hash)
            == (reference.get("Bucket"), reference.get("Hash"))
        )
        if (
            cached is not None
            and same_logo
            and all(getattr(cached, field) == value for field, value in values.items())
        ):
            # All observations in this response share one timestamp; the first
            # read already advanced its watermark and discovered its resources.
            return cached
        with transaction.atomic():
            club, _ = Club.objects.select_for_update(no_key=True).get_or_create(
                external_id=source_id, defaults={"name": "", **values}
            )
            update_club_metadata(
                club,
                values,
                self.observed_at,
                fill_only=not self.discover,
            )
            if (
                self.discover
                and self._source_kind == "clubs"
                and (
                    club.current_directory_observed_at is None
                    or club.current_directory_observed_at < self.observed_at
                )
            ):
                club.current_directory_observed_at = self.observed_at
                club.save(update_fields=("current_directory_observed_at",))
        for kind in (
            (
                "club_teams",
                "club_program",
                "club_results",
                "club_contact",
                "club_sports",
            )
            if self.discover
            else ()
        ):
            enqueue(self.season, kind, club.external_id)
        if (
            self.discover
            and club.current_directory_observed_at is not None
            and not club.dissolved
        ):
            enqueue(self.season, "club_details", club.external_id)
        discover_logo(
            club,
            data.get("ClubLogo"),
            self.season,
            observed_at=self.observed_at,
            fill_only=not self.discover,
        )
        self._clubs[source_id] = club
        return club

    def team(self, data: dict[str, Any]) -> Team:
        """Preserve source team identity, sport and season.

        Raises:
            ValueError: The team conflicts with its retained source club group.

        """
        source_id = str(data["PublicTeamId"])
        if settings.SPORTLINK_SPLIT_SEASONS and not self._seasons_configured:
            configure_seasons(
                self.season,
                self.season.start_date.year,
                split_outdoor=getattr(
                    settings, "SPORTLINK_SPLIT_OUTDOOR_PHASES", False
                ),
            )
            self._seasons_configured = True
        cached = self._teams.get(source_id)
        incoming_club_id = str(data["Club"]["ClubId"])
        if cached is not None and cached.club.external_id != incoming_club_id:
            raise ValueError("Conflicting duplicate team club identity")
        if cached is not None:
            club = self.club(club_with_name(data))
            cached_values = {
                "name": data["TeamName"],
                "club_id": club.pk,
                "sport": data.get("SportId") or "",
                "source_context": merge_source_context(
                    "team",
                    cached.source_context,
                    data,
                    self.observed_at,
                    self._source_kind,
                ),
            }
            if all(
                getattr(cached, field) == value
                for field, value in cached_values.items()
            ):
                return cached
        # Validate the retained group before the incoming club can mutate the
        # global catalogue. Native joint-registration ownership is independent.
        existing = (
            Team.objects
            .select_related("group__club")
            .filter(season=self.season, external_id=source_id)
            .first()
        )
        if (
            existing
            and existing.group
            and (existing.group.club.external_id != incoming_club_id)
        ):
            raise ValueError("Team group club identity conflict")
        club = self.club(club_with_name(data))
        values = {
            "name": data["TeamName"],
            "club_id": club.pk,
            "sport": data.get("SportId") or "",
        }
        with transaction.atomic():
            team, _ = Team.objects.select_for_update(no_key=True).get_or_create(
                season=self.season, external_id=source_id, defaults=values
            )
            if (
                team.group_id
                and TeamGroup.objects
                .filter(pk=team.group_id)
                .exclude(club=club)
                .exists()
            ):
                raise ValueError("Team group club identity conflict")
            previous_gender = context_value(team.source_context, "Gender")
            values["source_context"] = merge_source_context(
                "team", team.source_context, data, self.observed_at, self._source_kind
            )
            if team.sport != values["sport"]:
                team.local_team_data = None
                team.save(update_fields=("local_team_data",))
            save_changed(team, values)
            team.club = club
            if context_value(team.source_context, "Gender") != previous_gender:
                self._touched_pool_ids.update(
                    PoolEntry.objects.filter(team=team).values_list(
                        "pool_id", flat=True
                    )
                )
                self._touched_pool_ids.update(
                    Match.objects
                    .filter(models.Q(home_team=team) | models.Q(away_team=team))
                    .exclude(pool=None)
                    .values_list("pool_id", flat=True)
                )
        if team.group_id is None:
            team.group, _ = TeamGroup.objects.get_or_create(
                season=self.season,
                club_id=club.pk,
                normalized_name=team_group_key(team.name, club.name),
                defaults={"name": team.name},
            )
            team.save(update_fields=("group",))
        self._queue_team(team)
        self._teams[source_id] = team
        return team

    def _queue_team(self, team: Team) -> None:
        """Discover configured current-team resources once per response entity."""
        if self.discover:
            enqueue(self.season, "team_pools", team.external_id)
            if getattr(settings, "SPORTLINK_IMPORT_ROSTERS", False):
                enqueue(self.season, "team_roster", team.external_id)

    def pool(self, data: dict[str, Any], sport: str = "") -> Pool:
        """Upsert a poule without blank summary fields erasing known metadata."""
        source_id = str(data["PoolId"])
        values = {
            "name": data.get("PoolName"),
            "class_name": data.get("ClassName"),
            "sport": sport,
        }
        values = {key: value for key, value in values.items() if value}
        cached = self._pools.get(source_id)
        if cached is not None:
            values["source_context"] = merge_source_context(
                "pool", cached.source_context, data, self.observed_at, self._source_kind
            )
        if cached is not None and all(
            getattr(cached, key) == value for key, value in values.items()
        ):
            return cached
        with transaction.atomic():
            pool, _ = Pool.objects.select_for_update(no_key=True).get_or_create(
                season=self.season, external_id=source_id, defaults=values
            )
            values["source_context"] = merge_source_context(
                "pool", pool.source_context, data, self.observed_at, self._source_kind
            )
            changed_context = any(
                context_value(pool.source_context, key)
                != context_value(values["source_context"], key)
                for key in ("CompetitionKind", "SortOrder")
            ) or any(
                key in values and getattr(pool, key) != values[key]
                for key in ("class_name", "sport")
            )
            save_changed(pool, values)
            if changed_context:
                invalidate_pool_metadata(pool.pk)
            map_pool(pool)
        if self.discover:
            enqueue(self.season, "pool_results", pool.external_id)
        self._pools[source_id] = pool
        return pool

    def rejects_fixture_observation(self, match: Match, *, result: bool) -> bool:
        """Reject obsolete nested catalog inputs before writing their identities."""
        previous = match.result_observed_at
        if result:
            return previous is not None and previous > self.observed_at
        protected = (
            (match.status == "FINAL" and previous is not None)
            or match.home_score is not None
            or match.away_score is not None
        )
        return protected or (previous is not None and previous >= self.observed_at)

    def _existing_fixture(
        self, source_id: str, *, result: bool
    ) -> tuple[Match | None, bool]:
        """Read and guard the fixture before accepting its nested catalog inputs."""
        match = (
            Match.objects
            .select_for_update(no_key=True)
            .filter(season=self.season, external_id=source_id)
            .first()
        )
        if match is not None:
            self.observed_match_ids.add(match.pk)
            return match, not self.rejects_fixture_observation(match, result=result)
        return None, True

    def match(self, data: dict[str, Any], *, result: bool) -> None:
        """Upsert a fixture and retain score revisions from result feeds only.

        Raises:
            ValueError: Match teams or timestamp are inconsistent.

        """
        starts_at = parse_datetime(data["MatchDateTime"])
        if starts_at is None or timezone.is_naive(starts_at):
            raise ValueError("MatchDateTime must contain a timezone")
        if not self.window[0] <= timezone.localdate(starts_at) <= self.window[1]:
            return
        if _self_fixture(data) or self._repeated_match(data, starts_at, result=result):
            return
        lock_fixture_pools([self.season], [data])
        match, accepted = self._existing_fixture(
            str(data["PublicMatchId"]), result=result
        )
        if not accepted:
            return
        old_pool_id = getattr(match, "pool_id", None)
        old_standing_inputs = standing_inputs(match)
        home = self.team(data["HomeTeam"])
        away = self.team(data["AwayTeam"])
        if home.sport != away.sport:
            raise ValueError("Inconsistent match teams")
        values = {
            "home_team_id": home.pk,
            "away_team_id": away.pk,
            "starts_at": starts_at,
        }
        if data.get("Pool"):
            values["pool_id"] = self.pool(data["Pool"], home.sport).pk
            for member in (home, away):
                self.enter_pool(values["pool_id"], member)
        created = False
        if match is None:
            match, created = Match.objects.get_or_create(
                season=self.season,
                external_id=str(data["PublicMatchId"]),
                defaults={**values, "status": data["Status"]},
            )
        self.observed_match_ids.add(match.pk)
        self.discover_lineup(match)
        if self.rejects_fixture_observation(match, result=result):
            return
        self._fixture_content(match, data, values, result=result, created=created)
        self._record_standing_change(match, old_pool_id, old_standing_inputs)

    def _fixture_content(
        self,
        match: Match,
        data: dict[str, Any],
        values: dict[str, Any],
        *,
        result: bool,
        created: bool,
    ) -> None:
        """Bind source fixture content and metadata observations to accepted inputs."""
        contexts = {
            kind: metadata_context(match, kind)
            for kind in ("match_timing", "match_facility", "match_rules")
        }
        values["source_context"] = merge_source_context(
            "match",
            match.source_context,
            data,
            self.observed_at,
            "historical"
            if not self.discover
            else getattr(self, "_source_kind", "summary"),
        )
        if result:
            self._result(
                match,
                data,
                created=created,
                fixture_values=values,
                metadata_contexts=contexts,
            )
        else:
            self._program(match, data, values, contexts)
        # Fixture inputs are stored before timing observations certify their context.
        import_playing_time(match, data, self.observed_at)
        import_observed_cup_fixture(match, data)
        self.discover_details(match)

    def _record_standing_change(
        self, match: Match, old_pool_id: int | None, previous: tuple[object, ...] | None
    ) -> None:
        """Queue both sides of a meaningful fixture move or result correction."""
        if previous != standing_inputs(match):
            self._generated_pool_ids.update(
                pk for pk in (old_pool_id, match.pool_id) if pk is not None
            )

    def _program(
        self,
        match: Match,
        data: dict[str, Any],
        values: dict[str, Any],
        metadata_contexts: dict[str, str],
    ) -> None:
        """Apply accepted schedule content and invalidate changed metadata inputs."""
        context_changed = any(
            context_value(match.source_context, key)
            != context_value(values["source_context"], key)
            for key in ("RoundNr", "ExternalMatchId")
        )
        fields = assign_changed(match, {**values, "status": data["Status"]})
        fields.extend(invalidate_metadata(match, metadata_contexts))
        if fields:
            match.save(
                update_fields=(
                    *fields,
                    *(
                        ("updated_at",)
                        if context_changed
                        or any(key != "source_context" for key in fields)
                        else ()
                    ),
                )
            )

    def enter_pool(self, pool_id: int, team: Team) -> None:
        """Record poule membership; a poule's rows repeat pairs, so check once."""
        if (pool_id, team.pk) not in self._entries:
            _, created = PoolEntry.objects.get_or_create(pool_id=pool_id, team=team)
            if created:
                self._touched_pool_ids.add(pool_id)
                self._generated_pool_ids.add(pool_id)
                self._membership_pool_ids.add(pool_id)
            self._entries.add((pool_id, team.pk))

    def discover_details(self, match: Match) -> None:
        """Enrich live source matches, reusing opted-in lineup detail requests."""
        if self.discover:
            self._detail_match_ids.add(match.pk)

    def discover_lineup(self, match: Match) -> None:
        """Enable one shared match-selection feed only for opted-in discovery."""
        if self.discover and settings.SPORTLINK_IMPORT_LINEUPS:
            SyncResource.objects.get_or_create(
                season=self.season,
                kind="match_lineup",
                source_id=match.external_id,
                defaults={
                    "next_sync_at": max(
                        timezone.now(), match.starts_at - timedelta(days=1)
                    )
                },
            )

    def _repeated_match(
        self, data: dict[str, Any], starts_at: datetime, *, result: bool
    ) -> bool:
        """Skip repeated rows while rejecting contradictory IDs in one response.

        Duplicate provider rows that disagree raise ValueError during comparison.

        """
        key = (str(data["PublicMatchId"]), result)
        fields = match_identity(data, result=result)
        previous = self._matches.get(key)
        if previous is not None:
            compare_match(previous, fields)
            context = merge_duplicate_match(previous["context_row"], data)
            context_changed = context != previous["context_row"]
            if (
                previous["pool"] is not None or fields["pool"] is None
            ) and not context_changed:
                return True
            fields["context_row"] = context
            if fields["pool"] is None:
                fields["pool"] = previous["pool"]
        else:
            fields["context_row"] = merge_duplicate_match(data, data)
        self._matches[key] = fields
        return False

    def _result(
        self,
        match: Match,
        data: dict[str, Any],
        *,
        created: bool,
        fixture_values: dict[str, Any],
        metadata_contexts: dict[str, str] | None = None,
    ) -> None:
        """Apply only newer observations, including score removals/cancellations.

        Raises:
            ValueError: A score is not a nonnegative integer or explicitly unknown.

        """
        if match.result_observed_at and match.result_observed_at > self.observed_at:
            return
        values = {
            "status": data["Status"],
            "home_score": (data.get("HomeResult") or {}).get("Score"),
            "away_score": (data.get("AwayResult") or {}).get("Score"),
            "automatic_result": data.get("AutoResult") is not None,
        }
        for field in ("home_score", "away_score"):
            score = values[field]
            if score is not None and (
                not isinstance(score, int) or isinstance(score, bool) or score < 0
            ):
                raise ValueError("Match scores must be nonnegative integers or null")
        changed = created or any(
            getattr(match, key) != value for key, value in values.items()
        )
        context_changed = "source_context" in fixture_values and any(
            context_value(match.source_context, key)
            != context_value(fixture_values["source_context"], key)
            for key in ("RoundNr", "ExternalMatchId")
        )
        fields = assign_changed(match, {**fixture_values, **values})
        fields.extend(invalidate_metadata(match, metadata_contexts or {}))
        if context_changed or any(key != "source_context" for key in fields):
            fields.append("updated_at")
        # Freshness advances even for identical scores, but only changed match
        # content invalidates publication and the ratings fingerprint.
        fields.extend(
            assign_changed(
                match,
                {
                    "result_observed_at": self.observed_at,
                    "results_checked_at": self.observed_at,
                },
            )
        )
        if fields:
            match.save(update_fields=fields)
        if changed:
            ResultRevision.objects.create(
                match=match, observed_at=self.observed_at, **values
            )

    def assignments(self, data: dict[str, Any], source_id: str) -> None:
        """Discover poules and members from a team's published assignments."""
        team = Team.objects.get(season=self.season, external_id=source_id)
        for row in data["TeamPool"]:
            pool = self.pool(row, team.sport)
            self.enter_pool(pool.pk, team)
            for member in (row.get("PoolAssignment") or {}).get(
                "TeamInPoolAssignment", []
            ):
                self.enter_pool(pool.pk, self.team(member))

    def standings(self, data: dict[str, Any], source_id: str) -> None:
        """Replace official standing values atomically, without inferring points."""
        table = data.get("PoolStanding")
        rows = unique_standings(
            (table or {}).get("PoolStandingTeam") or [], require_counts=False
        )
        pool = Pool.objects.get(season=self.season, external_id=source_id)
        lock_fixture_pools(
            [self.season],
            [
                {**row, "Pool": {**(row.get("Pool") or {}), "PoolId": source_id}}
                for row in data["MatchResult"]
            ],
        )
        for row in data["MatchResult"]:
            self.match(row, result=True)
        if table is None:
            # An absent table supplies no replacement authority.
            return
        pool = Pool.objects.select_for_update(no_key=True).get(pk=pool.pk)
        if pool.standings_synced_at and pool.standings_synced_at > self.observed_at:
            return
        # Compare the complete table once: unchanged polling must not rewrite
        # every membership, while missing rows still lose their old standings.
        standings = {
            self.team(row).pk: {key: row[key] for key in STANDING_FIELDS if key in row}
            for row in rows
        }
        existing = {
            entry.team_id: entry
            for entry in PoolEntry.objects
            .select_for_update(no_key=True)
            .filter(pool=pool)
            .order_by("pk")
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
        filtered = data.get("ResultsFiltered") is not False
        provenance = dict(pool.standings_provenance)
        digest = content_digest(
            (team_id, standings.get(team_id, {}))
            for team_id in existing.keys() | standings.keys()
        )
        meaningful = (
            bool(changed)
            or pool.results_filtered != filtered
            or (
                provenance.get("official_digest") is not None
                and provenance["official_digest"] != digest
            )
        )
        if meaningful:
            provenance.pop("official", None)
            self._generated_pool_ids.add(pool.pk)
            if any(entry.team_id not in existing for entry in changed):
                self._touched_pool_ids.add(pool.pk)
        provenance["official_digest"] = digest
        pool.standings_synced_at = self.observed_at
        pool.results_filtered = filtered
        fields = ["standings_synced_at", "results_filtered"]
        if provenance != pool.standings_provenance:
            pool.standings_provenance = provenance
            fields.append("standings_provenance")
        pool.save(update_fields=fields)

    @transaction.atomic
    def apply(self, kind: str, source_id: str, data: dict[str, Any]) -> None:
        """Reject unknown envelopes and roll back malformed collection responses.

        Raises:
            ValueError: The provider response or resource kind is invalid.

        """
        self._clubs.clear()
        self._teams.clear()
        self._pools.clear()
        self._matches.clear()
        self._entries.clear()
        self.observed_match_ids.clear()
        self._detail_match_ids.clear()
        self._touched_pool_ids.clear()
        self._generated_pool_ids.clear()
        self._membership_pool_ids.clear()
        self._source_kind = kind if self.discover else "historical"
        if data.get("Error"):
            raise ValueError("Sportlink returned an application error")
        if kind in {"clubs", "club_teams"}:
            self._apply_collection(kind, data)
        elif kind in SOURCE_IMPORTS:
            SOURCE_IMPORTS[kind](source_id, data)
        elif kind in {"club_contact", "club_sports"}:
            {
                "club_contact": import_club_contact,
                "club_sports": import_club_sports,
            }[kind](source_id, data, self.observed_at)
        elif kind == "club_details":
            import_club_details(self.season, source_id, data, self.observed_at)
        elif kind in {
            "match_lineup",
            "team_roster",
            "match_timing",
            "match_facility",
            "match_rules",
        }:
            {
                "match_lineup": import_lineup,
                "team_roster": import_roster,
                "match_timing": import_timing_details,
                "match_facility": import_facility,
                "match_rules": import_rules,
            }[kind](
                self.season,
                source_id,
                data,
                self.observed_at,
                **(
                    {"expected_context": self.expected_metadata_context}
                    if kind in {"match_timing", "match_facility", "match_rules"}
                    else {}
                ),
            )
        elif kind == "team_pools":
            self.assignments(data, source_id)
        elif kind == "pool_results":
            self.standings(data, source_id)
        elif kind in {"club_program", "club_results"}:
            self.match_collection(kind, data)
        else:
            raise ValueError("Unsupported competition resource")
        self._refresh_touched_pools()
        self._queue_details()

    def _apply_collection(self, kind: str, data: dict[str, Any]) -> None:
        """Import directory collections only in their permitted time context.

        Raises:
            ValueError: Current ClubTeams was requested for historical context.

        """
        if kind == "club_teams" and (
            not self.discover
            or self.season.end_date < timezone.localdate(self.observed_at)
        ):
            raise ValueError("Current ClubTeams cannot populate historical context")
        field, import_row = {
            "clubs": ("Club", self.club),
            "club_teams": ("ClubTeam", self.team),
        }[kind]
        for row in data[field]:
            import_row(row)

    def _refresh_touched_pools(self) -> None:
        """Finalize only changed memberships/context and existing generated tables."""
        if self._membership_pool_ids:
            refresh_official_digests(self._membership_pool_ids)
        for pool in (
            Pool.objects
            .filter(pk__in=self._touched_pool_ids)
            .select_related("season")
            .order_by("pk")
        ):
            map_pool(pool)
        if self._generated_pool_ids:
            refresh_generated_standings(self._generated_pool_ids)

    def _queue_details(self) -> None:
        """Queue one response's missing metadata with bounded checkpoint queries."""
        if self._detail_match_ids:
            queue_missing_details(
                self.season,
                match_ids=self._detail_match_ids,
                include_timing=not settings.SPORTLINK_IMPORT_LINEUPS,
            )

    def match_collection(self, kind: str, data: dict[str, Any]) -> None:
        """Import the provider's fixture or result envelope."""
        is_result = kind == "club_results"
        key = "MatchResult" if is_result else "ProgramItemMatchClub"
        rows = [row if is_result else row["Match"] for row in data[key]]
        lock_fixture_pools([self.season], rows)
        for row in rows:
            self.match(row, result=is_result)
