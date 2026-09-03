from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def mark_existing_referee_goals(apps, schema_editor):
    tournament_result_audit = apps.get_model("tournament", "TournamentResultAudit")
    tournament_result_audit.objects.filter(
        reason="Goal recorded by referee tracker"
    ).update(source="referee_goal")


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0021_remove_playerclubmembership_pcm_player_idx_and_more"),
        ("tournament", "0004_tournamentmatch_away_qualifier_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="tournamentteam",
            name="referee_access_token",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="tournamentmatch",
            name="field_ready_by_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="tournamentmatch",
            name="referee_claim_token",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="tournamentmatch",
            name="referee_claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tournamentmatch",
            name="referee_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="tournamentmatch",
            name="referee_player",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="claimed_tournament_referee_matches",
                to="player.player",
            ),
        ),
        migrations.AddField(
            model_name="tournamentmatch",
            name="referee_team",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="referee_matches",
                to="tournament.tournamentteam",
            ),
        ),
        migrations.AlterField(
            model_name="tournamentresultaudit",
            name="changed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tournament_result_changes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="tournamentresultaudit",
            name="changed_by_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="tournamentresultaudit",
            name="source",
            field=models.CharField(
                choices=[
                    ("direct", "Direct result edit"),
                    ("referee_goal", "Referee goal"),
                    ("referee_undo", "Referee goal removal"),
                ],
                default="direct",
                max_length=24,
            ),
        ),
        migrations.RunPython(
            mark_existing_referee_goals,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
