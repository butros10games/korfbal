# Generated by Django 5.1.4 on 2024-12-23 13:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("player", "0003_alter_player_profile_picture"),
    ]

    operations = [
        migrations.AlterField(
            model_name="player",
            name="profile_picture",
            field=models.ImageField(
                blank=True, null=True, upload_to="profile_pictures/"
            ),
        ),
    ]
