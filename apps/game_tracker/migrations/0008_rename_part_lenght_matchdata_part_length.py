# Generated by Django 5.1.4 on 2025-01-28 15:05

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("game_tracker", "0007_rename_grouptypes_grouptype"),
    ]

    operations = [
        migrations.RenameField(
            model_name="matchdata",
            old_name="part_lenght",
            new_name="part_length",
        ),
    ]
