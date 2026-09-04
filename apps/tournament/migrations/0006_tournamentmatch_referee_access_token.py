from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tournament", "0005_tournament_referee_duties"),
    ]

    operations = [
        migrations.AddField(
            model_name="tournamentmatch",
            name="referee_access_token",
            field=models.UUIDField(
                blank=True,
                editable=False,
                null=True,
                unique=True,
            ),
        ),
    ]
