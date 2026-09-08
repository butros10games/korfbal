"""Join independent roster and allocation schema branches."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("competition", "0007_rostermembership"),
        ("competition", "0009_allocation_class_link"),
    ]

    operations = []
