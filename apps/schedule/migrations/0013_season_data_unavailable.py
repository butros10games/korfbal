"""Record seasons that no source supplies.

Sportlink stopped serving 2023-24 and the 2024 autumn season, and no other
source holds their matches. Marking them lets team and club pages show the
season as one without data instead of leaving it out.
"""

from django.db import migrations, models


def mark_known_gaps(apps, schema_editor):
    season = apps.get_model("schedule", "Season")
    season.objects.filter(
        models.Q(edition=2023) | models.Q(edition=2024, phase="autumn")
    ).update(data_unavailable=True)


class Migration(migrations.Migration):
    dependencies = [("schedule", "0012_season_competition_context")]

    operations = [
        migrations.AddField(
            model_name="season",
            name="data_unavailable",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text=(
                    "No source supplies this season's matches. Teams and clubs with "
                    "data before and after it list it as a season without data."
                ),
            ),
        ),
        migrations.RunPython(mark_known_gaps, migrations.RunPython.noop),
    ]
