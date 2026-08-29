from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "game_tracker",
            "0032_remove_matchdata_uniq_match_data_per_match_and_more",
        ),
    ]

    operations = [
        migrations.AlterField(
            model_name="playermatchimpact",
            name="impact_score",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0.000"),
                max_digits=9,
            ),
        ),
    ]
