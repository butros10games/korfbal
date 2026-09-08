"""Join independently added rating configuration and season/roster migrations."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("competition", "0011_ratingconfiguration"),
        ("competition", "0014_match_private_lineup_counts_and_more"),
    ]

    operations = []
