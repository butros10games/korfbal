# Generated by Django 5.1.1 on 2024-10-11 08:43

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("schedule", "0001_initial"),
        ("team", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="match",
            name="away_team",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="away_matches",
                to="team.team",
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="home_team",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="home_matches",
                to="team.team",
            ),
        ),
        migrations.AddField(
            model_name="match",
            name="season",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="matches",
                to="schedule.season",
            ),
        ),
    ]
