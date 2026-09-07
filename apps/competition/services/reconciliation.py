"""Link imported identities to existing records without rewriting recorded history."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from django.db import transaction
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


def joint_team_matches(
    source_name: str, source_club: str, local_club: str, local_team: str
) -> bool:
    """Recognize an exact joint-team identity registered under either partner."""
    local_partners = {normalized(part) for part in local_club.split("/")}
    if len(local_partners) <= 1 or "" in local_partners:
        return False
    suffix = " " + normalized(local_team)
    source_name = normalized(source_name)
    if not source_name.endswith(suffix):
        return False
    source_partners = {
        normalized(part) for part in source_name.removesuffix(suffix).split("/")
    }
    return (
        source_partners == local_partners and normalized(source_club) in source_partners
    )


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

    def __init__(self, overrides: dict[tuple[str, int], str], *, lock: bool) -> None:
        """Read the local catalogue once; lock records only when applying links."""
        self.overrides = overrides
        self.used: set[tuple[str, int]] = set()
        self.decisions: list[LinkDecision] = []
        self.sources = {}
        for kind, model in SOURCE_MODELS.items():
            query = model.objects.order_by("pk")
            if lock:
                query = query.select_for_update()
            self.sources[kind] = list(query.values())
        self.locals = {}
        for kind, model in {
            "club": LocalClub,
            "team": LocalTeam,
            "pool": SeasonPool,
            "match": LocalMatch,
        }.items():
            query = model.objects.order_by("pk")
            if lock:
                query = query.select_for_update()
            self.locals[kind] = {str(row["id_uuid"]): row for row in query.values()}

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
        club_links = self.choose(
            "club",
            {
                row["id"]: [
                    pk
                    for pk, local in clubs.items()
                    if normalized(row["name"]) == normalized(local["name"])
                ]
                for row in self.sources["club"]
            },
            {row["id"]: set(clubs) for row in self.sources["club"]},
        )
        source_clubs = {row["id"]: row for row in self.sources["club"]}
        teams = self.locals["team"]
        teams_by_club: dict[str, set[str]] = {}
        for pk, local in teams.items():
            teams_by_club.setdefault(str(local["club_id"]), set()).add(pk)
        joint_teams = {
            pk: row
            for pk, row in teams.items()
            if "/" in clubs[str(row["club_id"])]["name"]
        }
        joint_candidates = {
            row["id"]: {
                pk
                for pk, local in joint_teams.items()
                if joint_team_matches(
                    row["name"],
                    source_clubs[row["club_id"]]["name"],
                    clubs[str(local["club_id"])]["name"],
                    local["name"],
                )
            }
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
        source_teams = {
            row["id"]: team_links.get(row["group_id"])
            for row in Team.objects.values("id", "group_id")
        }
        pool_links = self.pools(source_teams)
        self.matches(source_teams, pool_links)
        if set(self.overrides) - self.used:
            raise ValueError("Mapping refers to an unknown source kind or ID")
        return self.decisions

    def pools(self, team_links: dict[int, str | None]) -> dict[int, str]:
        """Match poules by season, name and fully linked membership."""
        memberships: dict[int, set[str | None]] = {}
        for pool_id, team_id in PoolEntry.objects.values_list("pool_id", "team_id"):
            memberships.setdefault(pool_id, set()).add(team_links.get(team_id))
        local_members: dict[str, set[str]] = {}
        for pool in SeasonPool.objects.prefetch_related("teams"):
            local_members[str(pool.pk)] = {str(team.pk) for team in pool.teams.all()}
        allowed, candidates = {}, {}
        for row in self.sources["pool"]:
            allowed[row["id"]] = {
                pk
                for pk, local in self.locals["pool"].items()
                if local["season_id"] == row["season_id"]
            }
            members = memberships.get(row["id"], set())
            labels = {
                normalized(row["name"]),
                normalized(f"{row['class_name']} {row['name']}"),
            }
            candidates[row["id"]] = [
                pk
                for pk in allowed[row["id"]]
                if members
                and None not in members
                and members == local_members[pk]
                and normalized(self.locals["pool"][pk]["name"]) in labels
            ]
        return self.choose("pool", candidates, allowed)

    def matches(
        self, team_links: dict[int, str | None], pool_links: dict[int, str]
    ) -> None:
        """Require linked opponents, correct home/away, season and exact kickoff."""
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
