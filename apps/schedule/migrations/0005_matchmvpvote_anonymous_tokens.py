from __future__ import annotations

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("schedule", "0004_matchmvp_and_votes"),
    ]

    operations = [
        migrations.AddField(
            model_name="matchmvpvote",
            name="voter_token",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="matchmvpvote",
            name="voter",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="mvp_votes_cast",
                to="player.player",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="matchmvpvote",
            name="unique_mvp_vote_per_match_voter",
        ),
        migrations.AddConstraint(
            model_name="matchmvpvote",
            constraint=models.UniqueConstraint(
                fields=("match", "voter"),
                condition=Q(voter__isnull=False),
                name="unique_mvp_vote_per_match_voter",
            ),
        ),
        migrations.AddConstraint(
            model_name="matchmvpvote",
            constraint=models.UniqueConstraint(
                fields=("match", "voter_token"),
                condition=Q(voter_token__isnull=False),
                name="unique_mvp_vote_per_match_voter_token",
            ),
        ),
        migrations.AddConstraint(
            model_name="matchmvpvote",
            constraint=models.CheckConstraint(
                condition=(
                    (Q(voter__isnull=False) & Q(voter_token__isnull=True))
                    | (Q(voter__isnull=True) & Q(voter_token__isnull=False))
                ),
                name="mvp_vote_requires_voter_or_token",
            ),
        ),
        migrations.AddIndex(
            model_name="matchmvpvote",
            index=models.Index(fields=["voter_token"], name="schedule_mvpvote_token_idx"),
        ),
    ]
