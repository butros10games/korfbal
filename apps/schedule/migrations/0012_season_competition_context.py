"""Store each season's competition context explicitly.

Existing seasons receive an edition only when both dates fall inside one
July-June korfbal year. Discipline and phase are recorded only for seasons the
competition importer created, recognised by their canonical name *and*
consistent dates; every other season stays unresolved for review. The rules
are frozen here so later application changes cannot alter this migration.
"""

from datetime import date

from django.db import migrations, models


FIRST_MONTH = 7
DISCIPLINE = {
    "autumn": "outdoor",
    "spring": "outdoor",
    "full_season": "outdoor",
    "indoor": "indoor",
}


def _edition(day: date) -> int:
    return day.year if day.month >= FIRST_MONTH else day.year - 1


def _edition_from_dates(start: date, end: date) -> int | None:
    edition = _edition(start)
    return edition if _edition(end) == edition else None


def _canonical(name: str, start: date, end: date) -> tuple[int, str] | None:
    words = name.strip().split()
    if len(words) != 3 or words[1].casefold() != "seizoen":
        return None
    edition = _edition_from_dates(start, end)
    if edition is None:
        return None
    expected = {
        "voor": ("autumn", str(edition)),
        "na": ("spring", str(edition + 1)),
        "zaal": ("indoor", f"{edition}-{edition + 1}"),
        "veld": ("full_season", f"{edition}-{edition + 1}"),
    }.get(words[0].casefold())
    if expected is None or expected[1] != words[2]:
        return None
    if expected[0] == "autumn" and start.month < FIRST_MONTH:
        return None
    if expected[0] == "spring" and start.month >= FIRST_MONTH:
        return None
    return edition, expected[0]


def record_context(apps, schema_editor):
    seasons = apps.get_model("schedule", "Season").objects.using(
        schema_editor.connection.alias
    )
    changed = []
    for season in seasons.filter(edition__isnull=True).only(
        "pk", "name", "start_date", "end_date"
    ):
        season.edition = _edition_from_dates(season.start_date, season.end_date)
        canonical = _canonical(season.name, season.start_date, season.end_date)
        if canonical is not None:
            season.phase = canonical[1]
            season.discipline = DISCIPLINE[canonical[1]]
            season.context_source = "legacy_name"
        if season.edition is not None or canonical is not None:
            changed.append(season)
    seasons.bulk_update(
        changed,
        ["edition", "discipline", "phase", "context_source"],
        batch_size=500,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("schedule", "0011_match_schedule_match_distinct_teams_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="season",
            name="edition",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Start year of the July-June korfbal year (2025 for 2025-2026).",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="season",
            name="discipline",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                choices=[("indoor", "indoor"), ("outdoor", "outdoor")],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="season",
            name="phase",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                choices=[
                    ("autumn", "autumn"),
                    ("spring", "spring"),
                    ("full_season", "full_season"),
                    ("indoor", "indoor"),
                ],
                help_text="autumn/spring halves, full_season for continuous outdoor play.",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="season",
            name="context_source",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                choices=[
                    ("importer", "importer"),
                    ("legacy_name", "legacy_name"),
                    ("manual", "manual"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="season",
            constraint=models.CheckConstraint(
                condition=models.Q(("edition__isnull", True))
                | models.Q(("edition__gte", 1900), ("edition__lte", 2999)),
                name="season_plausible_edition",
            ),
        ),
        migrations.RunPython(record_context, migrations.RunPython.noop),
    ]
