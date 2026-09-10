"""Join the independent cup and sync monitoring schema branches."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("competition", "0019_cupcompetition_cupfixture_and_more"),
        ("competition", "0019_sync_run_monitoring"),
    ]

    operations = []
