"""Link imported identities to existing records without rewriting recorded history."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.club.models.club import Club as LocalClub
from apps.competition.models import (
    Club,
    Match,
    Pool,
    PoolEntry,
    SyncLease,
    Team,
    TeamGroup,
)
from apps.competition.services.seasons import SeasonResolver
from apps.schedule.models import (
    Match as LocalMatch,
    SeasonPool,
)
from apps.team.models.team import Team as LocalTeam


SOURCE_MODELS = {"club": Club, "team": TeamGroup, "pool": Pool, "match": Match}
LOCAL_FIELDS = {
    "club": "local_club",
    "team": "local_team",
    "pool": "local_pool",
    "match": "local_match",
}


def normalized(value: str) -> str:
    """Ignore only case and whitespace; retain age groups and team numbers."""
    return " ".join(value.casefold().split())


def team_label(name: str, club_name: str) -> str:
    """Allow a full provider team name to match a local number within a linked club."""
    name, prefix = normalized(name), normalized(club_name) + " "
    return name.removeprefix(prefix)


class JointTeamIndex:
    """Look up exact partner sets and designations without pairwise comparisons."""

    def __init__(self) -> None:
        """Keep all candidates so ambiguous identities remain ambiguous."""
        self.entries: dict[tuple[frozenset[str], str], set[str]] = defaultdict(set)

    def add(self, identifier: str, club: str, team: str) -> None:
        """Index the normalized native partner set and full team designation."""
        partners = frozenset(normalized(part) for part in club.split("/"))
        if len(partners) > 1 and "" not in partners:
            self.entries[partners, normalized(team)].add(identifier)

    def matches(self, source_name: str, source_club: str) -> set[str]:
        """Consider each word boundary, retaining support for multiword designations."""
        name, club = normalized(source_name), normalized(source_club)
        if "/" not in name:
            return set()
        candidates: set[str] = set()
        for position, character in enumerate(name):
            if character != " ":
                continue
            partners = frozenset(
                normalized(part) for part in name[:position].split("/")
            )
            if club in partners:
                candidates.update(
                    self.entries.get((partners, name[position + 1 :]), ())
                )
        return candidates


@dataclass
class LinkDecision:
    """Reviewable identity decision; unresolved candidates are never applied."""

    kind: str
    source_id: int
    source_name: str
    local_id: str | None
    candidates: list[str]
    reason: str
    external_id: str | None = None
    season_id: str | None = None
    city: str = ""


def validate_existing(
    kind: str, rows: list[dict[str, Any]], field: str, allowed: dict[int, set[str]]
) -> None:
    """Reject stale parent relationships without silently moving existing links.

    Raises:
        ValueError: A previously linked identity now contradicts its parent links.

    """
    for row in rows:
        if row[field] is not None and str(row[field]) not in allowed[row["id"]]:
            raise ValueError(
                f"Existing {kind} link for source {row['id']} "
                "conflicts with its parents"
            )


class Reconciler:
    """Resolve dependencies in memory so a preview performs no database writes."""

    def __init__(
        self,
        overrides: dict[tuple[str, int], str],
        *,
        lock: bool,
        incremental: bool = False,
        skip_locked: bool = False,
    ) -> None:
        """Read the local catalogue once; lock records only when applying links.

        ``incremental`` plans only unlinked (or explicitly overridden) source rows.
        Linked rows cannot change, so they are read as compact parent/claim links
        and native poules/matches only for the seasons the pending rows need.
        """
        self.overrides = overrides
        self.incremental = incremental
        self.lock = lock
        # Beside a running import, defer source rows it holds instead of waiting
        # on them: they publish next pass, and lock-order cycles cannot form.
        self.skip_locked = skip_locked
        self.used: set[tuple[str, int]] = set()
        self.decisions: list[LinkDecision] = []
        self.sources = {
            kind: self._source_rows(kind, lock=lock) for kind in SOURCE_MODELS
        }
        self.locals = {
            "club": self._local_rows(LocalClub.objects.all(), lock=lock),
            "team": self._local_rows(LocalTeam.objects.all(), lock=lock),
        }
        resolver = SeasonResolver()
        sports = {}
        if resolver.scopes:
            teams = Team.objects.all()
            if incremental:
                teams = teams.filter(
                    pk__in={row["home_team_id"] for row in self.sources["match"]}
                )
            sports = dict(teams.values_list("pk", "sport"))
        for kind, model in (("pool", SeasonPool), ("match", LocalMatch)):
            self._resolve_seasons(
                kind, model.objects.all(), resolver, sports, lock=lock
            )

    def _source_rows(self, kind: str, *, lock: bool) -> list[dict[str, Any]]:
        """Read source rows; incremental plans skip rows that are already linked."""
        query = SOURCE_MODELS[kind].objects.order_by("pk")
        if self.incremental:
            query = query.filter(
                Q(**{LOCAL_FIELDS[kind] + "__isnull": True})
                | Q(pk__in=[pk for key, pk in self.overrides if key == kind])
            )
        if lock:
            query = query.select_for_update(skip_locked=self.skip_locked, no_key=True)
        return list(query.values())

    def _resolve_seasons(
        self,
        kind: str,
        query: QuerySet,
        resolver: SeasonResolver,
        sports: dict[int, str],
        *,
        lock: bool,
    ) -> None:
        """Map source scopes to playing seasons and read their native rows."""
        field = LOCAL_FIELDS[kind] + "_id"
        targets = {}
        phases = (
            dict(
                Pool.objects.filter(
                    pk__in={row["pool_id"] for row in self.sources[kind]}
                ).values_list("pk", "phase")
            )
            if kind == "match" and resolver.split
            else {}
        )
        for row in self.sources[kind]:
            sport = row.get("sport", sports.get(row.get("home_team_id"), ""))
            phase = row.get("phase", phases.get(row.get("pool_id"), ""))
            targets[row["id"]] = (
                None
                if not phase
                and row.get("pool_id", row["id"]) is not None
                and resolver.splits(row["season_id"], sport)
                else resolver.resolve(row["season_id"], sport, phase)
            )
        if self.incremental:
            seasons = {
                target or row["season_id"]
                for row in self.sources[kind]
                for target in [targets[row["id"]]]
            }
            linked = {row[field] for row in self.sources[kind] if row[field]}
            if kind == "match":
                # A season holds tens of thousands of fixtures. Candidates need
                # linked opponents, so matches() reads them after team planning.
                query = query.filter(pk__in=linked)
            else:
                query = query.filter(Q(season_id__in=seasons) | Q(pk__in=linked))
        self.locals[kind] = self._local_rows(query, lock=lock)
        for row in self.sources[kind]:
            target = targets[row["id"]]
            local = self.locals[kind].get(str(row[field]))
            if target is not None and (not local or local["season_id"] == target):
                row["season_id"] = target

    @staticmethod
    def _local_rows(query: QuerySet, *, lock: bool) -> dict[str, dict[str, Any]]:
        """Read native rows keyed by UUID, locked when links will be applied."""
        query = query.order_by("pk")
        if lock:
            query = query.select_for_update(no_key=True)
        return {str(row["id_uuid"]): row for row in query.values()}

    def linked(
        self, kind: str, candidates: dict[int, list[str]]
    ) -> dict[int, tuple[str, str]]:
        """Read existing links that act as parents or claims for pending rows.

        Returns:
            Source ID to (claim scope, local ID) for rows not being planned.

        """
        field = LOCAL_FIELDS[kind] + "_id"
        rows = SOURCE_MODELS[kind].objects.exclude(**{field: None})
        if kind == "pool":
            # Claims on candidate poules, and the poules of pending matches.
            rows = rows.filter(
                Q(**{field + "__in": {t for c in candidates.values() for t in c}})
                | Q(pk__in={row["pool_id"] for row in self.sources["match"]})
            )
        elif kind == "match":
            rows = rows.filter(**{
                field + "__in": {t for c in candidates.values() for t in c}
            })
        scope = "season_id" if kind == "team" else field
        return {
            source: (str(season) if kind == "team" else "", str(local))
            for source, season, local in rows.values_list("pk", scope, field)
        }

    def include_linked(
        self,
        kind: str,
        candidates: dict[int, list[str]],
        chosen: dict[int, str],
        scopes: dict[int, str],
    ) -> None:
        """Add rows outside an incremental plan as parents and existing claims."""
        if self.incremental:
            for source, (scope, local) in self.linked(kind, candidates).items():
                chosen.setdefault(source, local)
                scopes.setdefault(source, scope)

    def choose(
        self, kind: str, candidates: dict[int, list[str]], allowed: dict[int, set[str]]
    ) -> dict[int, str]:
        """Accept only mutually unique candidates or explicit consistent mappings.

        Raises:
            ValueError: A manual mapping conflicts with existing identities or parents.

        """
        field = LOCAL_FIELDS[kind] + "_id"
        rows = self.sources[kind]
        validate_existing(kind, rows, field, allowed)
        chosen = {row["id"]: str(row[field]) for row in rows if row[field] is not None}
        scopes = {
            row["id"]: str(row["season_id"]) if kind == "team" else "" for row in rows
        }
        self.include_linked(kind, candidates, chosen, scopes)
        owners = {(scopes[source], local): source for source, local in chosen.items()}
        for row in rows:
            key = (kind, row["id"])
            if key not in self.overrides:
                continue
            self.used.add(key)
            target = self.overrides[key]
            if target not in allowed[row["id"]]:
                raise ValueError(
                    f"Invalid {kind} mapping for source {row['id']}; "
                    "check parent links and season"
                )
            existing = chosen.get(row["id"])
            owner = owners.get((scopes[row["id"]], target))
            if (existing and existing != target) or (
                owner is not None and owner != row["id"]
            ):
                raise ValueError(f"Conflicting {kind} mapping for source {row['id']}")
            chosen[row["id"]] = target
            owners[scopes[row["id"]], target] = row["id"]
        counts = Counter(
            (scopes[source], target)
            for source, targets in candidates.items()
            if source not in chosen
            for target in targets
        )
        for row in rows:
            source = row["id"]
            targets = sorted(candidates[source])
            reason = "unmatched"
            if source in chosen:
                reason = "linked" if row[field] else "explicit"
            elif (
                len(targets) == 1
                and counts[scopes[source], targets[0]] == 1
                and (scopes[source], targets[0]) not in owners
            ):
                chosen[source] = targets[0]
                owners[scopes[source], targets[0]] = source
                reason = "unique"
            elif targets:
                reason = "ambiguous_or_claimed"
            self.decisions.append(
                LinkDecision(
                    kind,
                    source,
                    str(row.get("name") or row.get("external_id") or source),
                    chosen.get(source),
                    targets,
                    reason,
                    row.get("external_id"),
                    str(row["season_id"]) if row.get("season_id") else None,
                    row.get("city", ""),
                )
            )
        return chosen

    def plan(self) -> list[LinkDecision]:
        """Build club/team/poule/match links across every imported season.

        Raises:
            ValueError: A mapping refers to an unknown source.

        """
        clubs = self.locals["club"]
        clubs_by_name: dict[str, list[str]] = defaultdict(list)
        for pk, local in clubs.items():
            clubs_by_name[normalized(local["name"])].append(pk)
        club_links = self.choose(
            "club",
            {
                # A dissolved club is never another club that shares its name.
                row["id"]: []
                if row["dissolved"]
                else clubs_by_name[normalized(row["name"])]
                for row in self.sources["club"]
            },
            {row["id"]: set(clubs) for row in self.sources["club"]},
        )
        source_clubs = {row["id"]: row for row in self.sources["club"]}
        if self.incremental:
            # Pending team groups often belong to clubs that are already linked.
            source_clubs.update(
                (row["id"], row)
                for row in Club.objects.filter(
                    pk__in={row["club_id"] for row in self.sources["team"]}
                    - source_clubs.keys()
                ).values("id", "name")
            )
        teams = self.locals["team"]
        teams_by_club: dict[str, set[str]] = {}
        for pk, local in teams.items():
            teams_by_club.setdefault(str(local["club_id"]), set()).add(pk)
        joint_index = JointTeamIndex()
        for pk, local in teams.items():
            joint_index.add(pk, clubs[str(local["club_id"])]["name"], local["name"])
        joint_candidates = {
            row["id"]: joint_index.matches(
                row["name"], source_clubs[row["club_id"]]["name"]
            )
            for row in self.sources["team"]
        }
        team_allowed = {
            row["id"]: teams_by_club.get(club_links.get(row["club_id"], ""), set())
            | joint_candidates[row["id"]]
            for row in self.sources["team"]
        }
        team_candidates = {
            row["id"]: [
                pk
                for pk in team_allowed[row["id"]]
                if pk in joint_candidates[row["id"]]
                or team_label(row["name"], source_clubs[row["club_id"]]["name"])
                == team_label(
                    teams[pk]["name"], clubs[str(teams[pk]["club_id"])]["name"]
                )
            ]
            for row in self.sources["team"]
        }
        team_links = self.choose("team", team_candidates, team_allowed)
        variants = Team.objects.all()
        if self.incremental:
            pools = [row["id"] for row in self.sources["pool"]]
            variants = variants.filter(
                Q(pk__in=PoolEntry.objects.filter(pool_id__in=pools).values("team_id"))
                | Q(
                    pk__in={
                        row[side]
                        for row in self.sources["match"]
                        for side in ("home_team_id", "away_team_id")
                    }
                )
            )
        source_teams = {
            row["id"]: team_links.get(row["group_id"])
            for row in variants.values("id", "group_id")
        }
        pool_links = self.pools(source_teams)
        self.matches(source_teams, pool_links)
        if set(self.overrides) - self.used:
            raise ValueError("Mapping refers to an unknown source kind or ID")
        return self.decisions

    def pools(self, team_links: dict[int, str | None]) -> dict[int, str]:
        """Match poules by season, name and fully linked membership."""
        entries = PoolEntry.objects.all()
        through = SeasonPool.teams.through.objects.all()
        if self.incremental:
            pending = [row["id"] for row in self.sources["pool"]]
            entries = entries.filter(pool_id__in=pending)
            through = through.filter(seasonpool_id__in=list(self.locals["pool"]))
        memberships: dict[int, set[str | None]] = {}
        for pool_id, team_id in entries.values_list("pool_id", "team_id"):
            memberships.setdefault(pool_id, set()).add(team_links.get(team_id))
        local_members: dict[str, set[str]] = defaultdict(set)
        for pool_id, team_id in through.values_list("seasonpool_id", "team_id"):
            local_members[str(pool_id)].add(str(team_id))
        by_season: dict[Any, set[str]] = defaultdict(set)
        index: dict[tuple[Any, str, frozenset[str]], list[str]] = defaultdict(list)
        for pk, local in self.locals["pool"].items():
            by_season[local["season_id"]].add(pk)
            index[
                local["season_id"],
                normalized(local["name"]),
                frozenset(local_members[pk]),
            ].append(pk)
        allowed, candidates = {}, {}
        for row in self.sources["pool"]:
            allowed[row["id"]] = by_season[row["season_id"]]
            members = memberships.get(row["id"], set())
            labels = {
                normalized(row["name"]),
                normalized(f"{row['class_name']} {row['name']}"),
            }
            labels.discard("")
            candidates[row["id"]] = []
            if members and None not in members:
                linked_members = frozenset(
                    member for member in members if member is not None
                )
                candidates[row["id"]] = [
                    pk
                    for label in labels
                    for pk in index.get((row["season_id"], label, linked_members), ())
                ]
        return self.choose("pool", candidates, allowed)

    def _read_match_candidates(self, team_links: dict[int, str | None]) -> None:
        """Read native fixtures between the linked opponents of pending matches."""
        keys = {
            (row["season_id"], home, away)
            for row in self.sources["match"]
            for home in [team_links.get(row["home_team_id"])]
            for away in [team_links.get(row["away_team_id"])]
            if home and away
        }
        if not keys:
            return
        query = LocalMatch.objects.filter(
            season_id__in={season for season, _, _ in keys},
            home_team_id__in={home for _, home, _ in keys},
            away_team_id__in={away for _, _, away in keys},
        )
        self.locals["match"].update(self._local_rows(query, lock=self.lock))

    def matches(
        self, team_links: dict[int, str | None], pool_links: dict[int, str]
    ) -> None:
        """Require linked opponents, correct home/away, season and exact kickoff."""
        if self.incremental:
            self._read_match_candidates(team_links)
        index: dict[tuple[str, str, str], list[str]] = {}
        for pk, local in self.locals["match"].items():
            key = (
                str(local["season_id"]),
                str(local["home_team_id"]),
                str(local["away_team_id"]),
            )
            index.setdefault(key, []).append(pk)
        allowed, candidates = {}, {}
        for row in self.sources["match"]:
            key = (
                str(row["season_id"]),
                team_links.get(row["home_team_id"]),
                team_links.get(row["away_team_id"]),
            )
            pool = pool_links.get(row["pool_id"])
            allowed[row["id"]] = {
                pk
                for pk in index.get(key, [])
                if not pool
                or self.locals["match"][pk]["pool_id"] is None
                or str(self.locals["match"][pk]["pool_id"]) == pool
            }
            candidates[row["id"]] = [
                pk
                for pk in allowed[row["id"]]
                if self.locals["match"][pk]["start_time"] == row["starts_at"]
            ]
        self.choose("match", candidates, allowed)


@transaction.atomic
def reconcile(
    *, apply: bool = False, overrides: dict[tuple[str, int], str] | None = None
) -> dict[str, Any]:
    """Preview or atomically link existing identities without provider traffic.

    Raises:
        ValueError: An importer is active; retry after its current batch.

    """
    if apply:
        lease, _ = SyncLease.objects.select_for_update().get_or_create(
            key="sportlink", defaults={"expires_at": timezone.now()}
        )
        if lease.owner is not None and lease.expires_at > timezone.now():
            raise ValueError("An import is running; reconcile after its current batch")
    planner = Reconciler(overrides or {}, lock=apply)
    decisions = planner.plan()
    if apply:
        for decision in decisions:
            if decision.reason in {"unique", "explicit"}:
                SOURCE_MODELS[decision.kind].objects.filter(
                    pk=decision.source_id
                ).update(**{LOCAL_FIELDS[decision.kind] + "_id": decision.local_id})
    linked = {
        kind: {row.local_id for row in decisions if row.kind == kind and row.local_id}
        for kind in SOURCE_MODELS
    }
    unlinked = {
        kind: [
            {"id": pk, "name": str(row.get("name") or row.get("start_time") or pk)}
            for pk, row in rows.items()
            if pk not in linked[kind]
        ]
        for kind, rows in planner.locals.items()
    }
    return {
        "applied": apply,
        "counts": dict(Counter(row.reason for row in decisions)),
        "decisions": [asdict(row) for row in decisions],
        "unlinked_local": unlinked,
    }
