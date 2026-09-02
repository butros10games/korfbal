"""Import an existing pool and fixture plan into tournament mode."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Max

from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.generation import ensure_schedule_replaceable


class ScheduleImportError(ValueError):
    """Raised when an imported fixture plan is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ImportedScheduleRow:
    """One pool match supplied by an existing tournament plan."""

    date: date
    start_time: time
    pool_name: str
    field_label: str
    home_team_name: str
    away_team_name: str
    duration_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class _PreparedRow:
    source_index: int
    starts_at: datetime
    duration_minutes: int
    pool_name: str
    field_label: str
    home_team_name: str
    away_team_name: str

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(minutes=self.duration_minutes)


def _key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _clean(value: str) -> str:
    return " ".join(value.split())


def _prepare_rows(
    tournament: Tournament,
    rows: list[ImportedScheduleRow],
) -> list[_PreparedRow]:
    if not rows:
        raise ScheduleImportError("Add at least one match to the imported schedule.")
    try:
        tournament_timezone = ZoneInfo(tournament.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleImportError("The tournament timezone is not available.") from exc

    prepared: list[_PreparedRow] = []
    team_pools: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        pool_name = _clean(row.pool_name)
        field_label = _clean(row.field_label)
        home_name = _clean(row.home_team_name)
        away_name = _clean(row.away_team_name)
        if _key(home_name) == _key(away_name):
            raise ScheduleImportError(f"Row {index} has the same home and away team.")
        pool_key = _key(pool_name)
        for team_name in (home_name, away_name):
            team_key = _key(team_name)
            previous_pool = team_pools.setdefault(team_key, pool_key)
            if previous_pool != pool_key:
                raise ScheduleImportError(
                    f'Row {index} assigns "{team_name}" to more than one pool.'
                )
        prepared.append(
            _PreparedRow(
                source_index=index,
                starts_at=datetime.combine(
                    row.date,
                    row.start_time,
                    tzinfo=tournament_timezone,
                ),
                duration_minutes=(
                    row.duration_minutes or tournament.match_duration_minutes
                ),
                pool_name=pool_name,
                field_label=field_label,
                home_team_name=home_name,
                away_team_name=away_name,
            )
        )

    _validate_windows(prepared)
    return sorted(
        prepared,
        key=lambda row: (row.starts_at, _key(row.field_label), row.source_index),
    )


def _validate_windows(rows: list[_PreparedRow]) -> None:
    field_windows: dict[str, list[_PreparedRow]] = {}
    team_windows: dict[str, list[_PreparedRow]] = {}
    for row in rows:
        field_windows.setdefault(_key(row.field_label), []).append(row)
        for team_name in (row.home_team_name, row.away_team_name):
            team_windows.setdefault(_key(team_name), []).append(row)

    for label, windows in (*field_windows.items(), *team_windows.items()):
        ordered = sorted(windows, key=lambda row: row.starts_at)
        for previous, current in pairwise(ordered):
            if previous.ends_at > current.starts_at:
                raise ScheduleImportError(
                    f"Rows {previous.source_index} and {current.source_index} "
                    f'overlap for "{label}".'
                )


def _objects_by_name[T](objects: list[T], attribute: str) -> dict[str, T]:
    result: dict[str, T] = {}
    for item in objects:
        value = str(getattr(item, attribute))
        key = _key(value)
        if key in result:
            raise ScheduleImportError(
                f'Multiple existing records match "{value}" case-insensitively.'
            )
        result[key] = item
    return result


@transaction.atomic
def apply_imported_schedule(
    tournament: Tournament,
    *,
    rows: list[ImportedScheduleRow],
) -> None:
    """Replace an unstarted schedule with an imported pool plan.

    Missing tournament teams and fields are created from the supplied labels.
    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    prepared = _prepare_rows(tournament, rows)
    ensure_schedule_replaceable(tournament)

    teams = _objects_by_name(list(tournament.teams.all()), "name")
    fields = _objects_by_name(list(tournament.fields.all()), "label")
    next_team_order = (
        tournament.teams.aggregate(value=Max("sort_order"))["value"] or 0
    ) + 1
    next_seed = (tournament.teams.aggregate(value=Max("seed"))["value"] or 0) + 1
    next_field_order = (
        tournament.fields.aggregate(value=Max("sort_order"))["value"] or 0
    ) + 1

    for row in prepared:
        for team_name in (row.home_team_name, row.away_team_name):
            team_key = _key(team_name)
            if team_key not in teams:
                team = TournamentTeam.objects.create(
                    tournament=tournament,
                    name=team_name,
                    short_name=team_name[:32],
                    seed=next_seed,
                    sort_order=next_team_order,
                )
                teams[team_key] = team
                next_seed += 1
                next_team_order += 1
        field_key = _key(row.field_label)
        if field_key not in fields:
            field = TournamentField.objects.create(
                tournament=tournament,
                label=row.field_label,
                sort_order=next_field_order,
            )
            fields[field_key] = field
            next_field_order += 1

    tournament.matches.all().delete()
    tournament.stages.all().delete()
    stage = TournamentStage.objects.create(
        tournament=tournament,
        name="Poules",
        kind=TournamentStage.Kind.POOL,
        sort_order=1,
    )
    pool_names: dict[str, str] = {}
    for row in prepared:
        pool_names.setdefault(_key(row.pool_name), row.pool_name)
    pools = {
        key: TournamentPool.objects.create(
            tournament=tournament,
            stage=stage,
            name=name,
            sort_order=index,
        )
        for index, (key, name) in enumerate(pool_names.items())
    }

    pool_team_names: dict[str, list[str]] = {key: [] for key in pools}
    for row in prepared:
        pool_key = _key(row.pool_name)
        for team_name in (row.home_team_name, row.away_team_name):
            team_key = _key(team_name)
            if team_key not in pool_team_names[pool_key]:
                pool_team_names[pool_key].append(team_key)
    TournamentPoolEntry.objects.bulk_create([
        TournamentPoolEntry(
            pool=pools[pool_key],
            team=teams[team_key],
            seed_order=index,
        )
        for pool_key, team_keys in pool_team_names.items()
        for index, team_key in enumerate(team_keys, start=1)
    ])

    round_numbers = {
        starts_at: index
        for index, starts_at in enumerate(
            sorted({row.starts_at for row in prepared}),
            start=1,
        )
    }
    TournamentMatch.objects.bulk_create([
        TournamentMatch(
            tournament=tournament,
            stage=stage,
            pool=pools[_key(row.pool_name)],
            home_team=teams[_key(row.home_team_name)],
            away_team=teams[_key(row.away_team_name)],
            field=fields[_key(row.field_label)],
            round_number=round_numbers[row.starts_at],
            match_number=index,
            starts_at=row.starts_at,
            duration_minutes=row.duration_minutes,
        )
        for index, row in enumerate(prepared, start=1)
    ])
