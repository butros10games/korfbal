"""Roster-naming tables: one clean 0010, from main and from the replaced draft.

Migration 0010 was amended before it reached ``main`` or any deployment (the
``korfbal-production`` marker and ``main`` end at 0009). A development database
that applied the earlier draft keeps a ``ClipIdentityReview`` keyed by job,
without ``id`` or ``section``, and Django sees 0010 as applied; it must migrate
back to 0009 first. These tests pin both the normal upgrade and that recovery.
"""

from typing import ClassVar

from django.conf import settings
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
import django.db.models.deletion
from django.db.utils import DatabaseError
import pytest


APP = "video_analysis"
BEFORE = [(APP, "0009_matchvideopublication_whistles")]
TARGET = [(APP, "0010_identity_review")]
REVIEW = "video_analysis_clipidentityreview"


class Draft(migrations.Migration):
    """The replaced draft of 0010 (commit f80621951), never on ``main``."""

    dependencies: ClassVar = [
        (APP, "0009_matchvideopublication_whistles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar = [
        migrations.CreateModel(
            name="ClipIdentityReview",
            fields=[
                (
                    "job",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="identity_review",
                        serialize=False,
                        to="video_analysis.analysisjob",
                    ),
                ),
                ("revision", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(default="preparing", max_length=16)),
                ("message", models.CharField(blank=True, max_length=300)),
                ("snapshot", models.JSONField(default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="IdentityReviewAnswer",
            fields=[
                (
                    "id",
                    models.UUIDField(editable=False, primary_key=True, serialize=False),
                ),
                ("sequence", models.PositiveIntegerField()),
                ("payload", models.JSONField(default=dict)),
                ("status", models.CharField(default="queued", max_length=16)),
                ("code", models.CharField(blank=True, max_length=24)),
                ("message", models.CharField(blank=True, max_length=300)),
                ("revision", models.PositiveIntegerField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(null=True)),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="identity_answers",
                        to="video_analysis.analysisjob",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["job", "status", "sequence"], name="va_answer_order"
                    )
                ]
            },
        ),
    ]


def columns() -> set[str]:
    """Return the review table's columns."""
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(cursor, REVIEW)
        }


def job(apps: object) -> object:
    """Create a clip job (and its workspace) through historical models."""
    user = apps.get_model("auth", "User").objects.create(username="migration-owner")  # type: ignore[attr-defined]
    workspace = apps.get_model(APP, "Workspace").objects.create(  # type: ignore[attr-defined]
        slug="migration", owner=user
    )
    return apps.get_model(APP, "AnalysisJob").objects.create(  # type: ignore[attr-defined]
        workspace=workspace, requested_by=user, kind="clip", status="completed"
    )


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_main_upgrades_to_section_reviews_keeping_its_jobs() -> None:
    """From main's 0009: existing jobs stay; reviews are per run and section."""
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(BEFORE)
        existing = job(executor.loader.project_state(BEFORE).apps)
        executor = MigrationExecutor(connection)
        executor.migrate(TARGET)
        current = executor.loader.project_state(TARGET).apps
        jobs = current.get_model(APP, "AnalysisJob")
        assert jobs.objects.get(pk=existing.pk).status == "completed"
        reviews = current.get_model(APP, "ClipIdentityReview")
        reviews.objects.create(job_id=existing.pk)
        reviews.objects.create(job_id=existing.pk, section="part-0001")
        assert sorted(reviews.objects.values_list("section", flat=True)) == [
            "",
            "part-0001",
        ]
        with connection.cursor() as cursor:
            # Older code paths writing without the column get the database default.
            cursor.execute(
                "INSERT INTO video_analysis_identityreviewanswer "
                "(id, job_id, sequence, payload, status, code, message, created_at)"
                " VALUES (%s, %s, 0, '{}', 'queued', '', '', CURRENT_TIMESTAMP)",
                ["00000000000000000000000000000001", existing.pk.hex],
            )
        answers = current.get_model(APP, "IdentityReviewAnswer")
        assert not answers.objects.get().section
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_a_database_with_the_replaced_draft_recovers_through_0009() -> None:
    """Review round 3, finding 6: the amended 0010 is not applied on top of the draft.

    Django records migrations by name, so a database that applied the draft has
    nothing left to do and keeps the draft's table. Migrating back to 0009 and
    forward again (the PR's rollout note) gives the current schema.
    """
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(BEFORE)
        state = executor.loader.project_state(BEFORE)
        draft = Draft("0010_identity_review", APP)
        with connection.schema_editor() as editor:
            draft.apply(state, editor)
        MigrationRecorder(connection).record_applied(*TARGET[0])
        executor = MigrationExecutor(connection)
        assert executor.migration_plan(TARGET) == []
        assert "section" not in columns()
        with pytest.raises(DatabaseError), connection.cursor() as cursor:
            cursor.execute("SELECT id, section FROM video_analysis_clipidentityreview")
        executor.migrate(BEFORE)
        assert REVIEW not in connection.introspection.table_names()
        executor = MigrationExecutor(connection)
        executor.migrate(TARGET)
        assert {"id", "job_id", "section"} <= columns()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
