# Generated by Django 5.1.1 on 2024-10-11 18:37

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0002_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="club",
            name="logo",
            field=models.ImageField(
                blank=True,
                default="/static/images/clubs/blank-club-picture.png",
                upload_to="club_pictures/",
            ),
        ),
    ]
