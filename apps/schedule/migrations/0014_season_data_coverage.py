"""Preserve populated legacy gaps without claiming complete source coverage."""

from django.db import migrations, models


def map_legacy_coverage(apps, schema_editor):
    seasons = apps.get_model("schedule", "Season").objects.using(schema_editor.connection.alias)
    native_matches = apps.get_model("schedule", "Match").objects.using(schema_editor.connection.alias)
    source_matches = apps.get_model("competition", "Match").objects.using(schema_editor.connection.alias)
    flagged = seasons.filter(data_unavailable=True)
    flagged.update(
        data_coverage="unavailable",
        coverage_reason="Legacy source gap: tested discovery supplied no fixtures; other source IDs remain unproven.",
    )
    candidates = flagged.values("pk")
    populated = models.Q(pk__in=native_matches.filter(season_id__in=candidates).values("season_id")) | models.Q(
        pk__in=source_matches.filter(season_id__in=candidates).values("season_id")
    )
    flagged.filter(populated).update(
        data_coverage="partial",
        data_unavailable=False,
        coverage_reason="Fixtures are available despite the legacy source gap; complete schedule coverage is unproven.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("schedule", "0013_season_data_unavailable"),
        ("competition", "0034_merge_competition_periods_history_lineup_cohort"),
    ]

    operations = [
        migrations.AddField(
            model_name="season",
            name="data_coverage",
            field=models.CharField(
                max_length=12,
                default="unknown",
                db_default="unknown",
                choices=[(value, value) for value in ("unknown", "partial", "complete", "unavailable")],
                help_text="Source-result coverage; complete requires reviewed schedule proof.",
            ),
        ),
        migrations.AddField(
            model_name="season",
            name="coverage_reason",
            field=models.CharField(
                max_length=255,
                blank=True,
                default="",
                db_default="",
                help_text="Public explanation of the coverage evidence and its scope.",
            ),
        ),
        migrations.AlterField(
            model_name="season",
            name="data_unavailable",
            field=models.BooleanField(
                default=False,
                db_default=False,
                help_text="Legacy no-data marker for older clients; available fixtures remain browsable even when discovery coverage is incomplete.",
            ),
        ),
        migrations.RunPython(map_legacy_coverage, migrations.RunPython.noop),
    ]
