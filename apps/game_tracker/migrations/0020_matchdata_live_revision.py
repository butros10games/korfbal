from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0019_tracker_timeline_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="matchdata",
            name="live_changed_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="matchdata",
            name="live_revision",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
