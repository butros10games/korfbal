# Generated by Django 5.1.1 on 2024-10-30 12:09

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('game_tracker', '0005_remove_matchdata_players_matchplayer'),
    ]

    operations = [
        migrations.AddField(
            model_name='grouptypes',
            name='order',
            field=models.IntegerField(default=0),
        ),
    ]