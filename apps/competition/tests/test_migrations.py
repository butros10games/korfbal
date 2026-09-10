"""Verify existing source variants are grouped by the real migration."""

from datetime import date

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_polling_upgrade_refills_only_collection_membership() -> None:
    """Legacy ETags cannot certify unknown membership after the schema upgrade."""
    executor = MigrationExecutor(connection)
    before = [("competition", "0017_schedule_notification_id")]
    after = [("competition", "0018_match_polling_coverage")]
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="coverage-migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        resources = old.get_model("competition", "SyncResource").objects
        now = timezone.now()
        for kind in ("club_program", "club_results", "pool_results", "clubs"):
            resources.create(
                season=season,
                kind=kind,
                etag="synthetic-validator",
                next_sync_at=now,
                fetched_at=now,
            )
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        current = executor.loader.project_state(after).apps
        resources = current.get_model("competition", "SyncResource").objects
        assert resources.get(kind="clubs").etag == "synthetic-validator"
        assert not resources.exclude(kind="clubs").exclude(etag="").exists()
        assert all(
            row.match_ids == [] and row.fetched_at == now and row.next_sync_at == now
            for row in resources.all()
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_existing_variants_are_backfilled() -> None:
    """Historical models exercise schema order and preserve source IDs."""
    executor = MigrationExecutor(connection)
    try:
        old_targets = [("competition", "0001_initial")]
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="group-migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        club = old.get_model("competition", "Club").objects.create(
            external_id="migration", name="Example"
        )
        team_model = old.get_model("competition", "Team")
        for source, name, sport in (
            ("ve", "Example 1", "VE"),
            ("za", " example  1 ", "ZA"),
            ("ve2", "Example 2", "VE"),
        ):
            team_model.objects.create(
                season=season, club=club, external_id=source, name=name, sport=sport
            )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        teams = current.get_model("competition", "Team").objects
        assert (
            teams.get(external_id="ve").group_id == teams.get(external_id="za").group_id
        )
        assert (
            teams.get(external_id="ve").group_id
            != teams.get(external_id="ve2").group_id
        )
        assert set(teams.values_list("external_id", flat=True)) == {"ve", "za", "ve2"}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_shared_identity_migrations_preserve_native_records() -> None:
    """Add source links and uniqueness without replacing native primary keys."""
    executor = MigrationExecutor(connection)
    old_targets = [
        (
            "competition",
            "0003_club_local_club_match_local_match_pool_local_pool_and_more",
        ),
        ("team", "0006_remove_teamdata_team_teamda_team_id_18931e_idx_and_more"),
        ("schedule", "0008_remove_match_schedule_ma_home_te_5e2ac9_idx_and_more"),
        ("game_tracker", "0035_player_match_impact_wpa"),
    ]
    try:
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        club = old.get_model("club", "Club").objects.create(
            name="Shared migration club"
        )
        team = old.get_model("team", "Team").objects.create(club=club, name="1")
        season = old.get_model("schedule", "Season").objects.create(
            name="shared migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        roster = old.get_model("team", "TeamData").objects.create(
            team=team, season=season, competition="Keep my competition", team_rank=3
        )
        source = old.get_model("competition", "Club").objects.create(
            external_id="shared-migration",
            name="Shared migration club",
            local_club_id=club.pk,
        )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        native = current.get_model("team", "TeamData").objects.get(pk=roster.pk)
        assert native.team_id == team.pk
        assert native.season_id == season.pk
        assert native.competition == "Keep my competition"
        assert (
            current
            .get_model("competition", "Club")
            .objects.get(pk=source.pk)
            .local_club_id
            == club.pk
        )
        assert {
            constraint.name
            for constraint in current.get_model("team", "TeamData")._meta.constraints
        } == {"unique_team_data_per_season"}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_logo_migration_requeues_only_club_lists() -> None:
    """Discover missing logo metadata without resetting completed match feeds."""
    executor = MigrationExecutor(connection)
    try:
        old_targets = [
            ("competition", "0004_match_local_created_match_published_at_and_more")
        ]
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="logos", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
        )
        stamp = timezone.now()
        resource = old.get_model("competition", "SyncResource")
        for kind in ("clubs", "club_results"):
            resource.objects.create(
                season=season,
                kind=kind,
                source_id="",
                fetched_at=stamp,
                next_sync_at=stamp,
                etag="old",
            )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        resource = executor.loader.project_state(targets).apps.get_model(
            "competition", "SyncResource"
        )
        assert (
            resource.objects.get(season_id=season.pk, kind="clubs").fetched_at is None
        )
        assert not resource.objects.get(season_id=season.pk, kind="clubs").etag
        assert (
            resource.objects.get(season_id=season.pk, kind="club_results").fetched_at
            == stamp
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_classification_migration_preserves_pool_identity() -> None:
    """Schema expansion preserves existing native/source IDs and membership."""
    executor = MigrationExecutor(connection)
    old_targets = [
        ("competition", "0006_trafficstate_rate_limited_historicalresource_and_more")
    ]
    try:
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="mapping-migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        native = old.get_model("schedule", "SeasonPool").objects.create(
            season=season, name="Original pool", sport="KORFBALL-VE-WK"
        )
        source = old.get_model("competition", "Pool").objects.create(
            season=season,
            external_id="preserved",
            name="01",
            class_name="Hoofdklasse",
            local_pool=native,
        )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        pool = current.get_model("competition", "Pool").objects.get(pk=source.pk)
        assert pool.local_pool_id == native.pk
        assert pool.class_name == "Hoofdklasse"
        assert pool.mapping_status == "unresolved"
        assert pool.competition_class_id is None
        assert (
            current.get_model("schedule", "SeasonPool").objects.get(pk=native.pk).name
            == "Original pool"
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_native_player_migration_preserves_accounts_and_rosters() -> None:
    """Existing player UUIDs and memberships survive making the login optional."""
    executor = MigrationExecutor(connection)
    old_targets = [
        ("competition", "0006_trafficstate_rate_limited_historicalresource_and_more"),
        ("player", "0021_remove_playerclubmembership_pcm_player_idx_and_more"),
    ]
    try:
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        user = old.get_model("auth", "User").objects.create(username="migration-player")
        player = old.get_model("player", "Player").objects.create(
            user=user, profile_picture="profile_pictures/existing.png"
        )
        season = old.get_model("schedule", "Season").objects.create(
            name="native-roster-migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        club = old.get_model("club", "Club").objects.create(name="Migration club")
        team = old.get_model("team", "Team").objects.create(
            name="Migration 1", club=club
        )
        data = old.get_model("team", "TeamData").objects.create(
            team=team, season=season
        )
        data.players.add(player)
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        players = current.get_model("player", "Player").objects
        migrated = players.get(pk=player.pk)
        assert migrated.user_id == user.pk
        assert migrated.profile_picture.name == "profile_pictures/existing.png"
        assert not migrated.knkv_photo
        assert not current.get_model("competition", "SeasonBinding").objects.exists()
        imported = players.create(name="Example", knkv_person_id="synthetic-person")
        assert imported.user_id is None
        data = current.get_model("team", "TeamData").objects.get(pk=data.pk)
        assert not data.staff.exists()
        assert not current.get_model("competition", "MatchMembership").objects.exists()
        data.players.add(imported)
        assert set(data.players.values_list("pk", flat=True)) == {
            player.pk,
            imported.pk,
        }
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_rating_configuration_migration_does_not_activate_existing_seasons() -> None:
    """Installing the schema preserves snapshots and requires explicit activation."""
    executor = MigrationExecutor(connection)
    try:
        old_targets = [("competition", "0010_merge_rosters_and_allocations")]
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="rating-publication-migration",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        source = old.get_model("competition", "AllocationSource").objects.create(
            season=season,
            digest="migration",
            label="Synthetic",
            published_on=date(2026, 9, 3),
        )
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        current = executor.loader.project_state(targets).apps
        assert not current.get_model(
            "competition", "RatingConfiguration"
        ).objects.exists()
        assert (
            current
            .get_model("competition", "AllocationSource")
            .objects.get(pk=source.pk)
            .digest
            == "migration"
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_schedule_baseline_migration_seeds_only_published_snapshots() -> None:
    """Existing fixtures retain identity and a baseline without replaying imports."""
    executor = MigrationExecutor(connection)
    before = [("competition", "0015_merge_rating_configuration_and_roster_counts")]
    after = [("competition", "0017_schedule_notification_id")]
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        season = old.get_model("schedule", "Season").objects.create(
            name="baseline-season",
            start_date=date(2026, 7, 1),
            end_date=date(2027, 6, 30),
        )
        club = old.get_model("competition", "Club").objects.create(
            external_id="baseline-club", name="Example"
        )
        local_club = old.get_model("club", "Club").objects.create(name="Example")
        native_team = old.get_model("team", "Team")
        home = native_team.objects.create(club=local_club, name="1")
        away = native_team.objects.create(club=local_club, name="2")
        source_team = old.get_model("competition", "Team")
        source_home = source_team.objects.create(
            season=season, club=club, external_id="home", name="1", sport="VE"
        )
        source_away = source_team.objects.create(
            season=season, club=club, external_id="away", name="2", sport="VE"
        )
        stamp = timezone.now()
        local = old.get_model("schedule", "Match").objects.create(
            season=season, home_team=home, away_team=away, start_time=stamp
        )
        source = old.get_model("competition", "Match")
        published = source.objects.create(
            season=season,
            external_id="published",
            home_team=source_home,
            away_team=source_away,
            starts_at=stamp,
            status="SCHEDULED",
            local_match=local,
        )
        source.objects.filter(pk=published.pk).update(published_at=timezone.now())
        pending = source.objects.create(
            season=season,
            external_id="pending",
            home_team=source_home,
            away_team=source_away,
            starts_at=stamp,
            status="SCHEDULED",
        )
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        updated = executor.loader.project_state(after).apps.get_model(
            "competition", "Match"
        )
        assert updated.objects.get(pk=published.pk).published_schedule == {
            "starts_at": stamp.isoformat(),
            "status": "SCHEDULED",
        }
        assert updated.objects.get(pk=published.pk).local_match_id == local.pk
        assert updated.objects.get(pk=published.pk).schedule_notification_id is None
        assert updated.objects.get(pk=pending.pk).published_schedule == {}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.migration_regression
@pytest.mark.django_db(transaction=True)
def test_cup_schema_preserves_existing_tournament_results() -> None:
    """Cup extensions preserve native scores and default ordinary events to no cup."""
    executor = MigrationExecutor(connection)
    before = [
        ("competition", "0018_match_polling_coverage"),
        ("tournament", "0008_tournamentdisplayconfig_show_sponsors"),
    ]
    after = [("competition", "0019_cupcompetition_cupfixture_and_more")]
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        owner = old.get_model(settings.AUTH_USER_MODEL).objects.create(
            username="cup-migration-owner"
        )
        tournament = old.get_model("tournament", "Tournament").objects.create(
            name="Existing event",
            slug="existing-cup-migration",
            owner=owner,
            starts_at=timezone.now(),
        )
        stage = old.get_model("tournament", "TournamentStage").objects.create(
            tournament=tournament, name="Finale", kind="final"
        )
        match = old.get_model("tournament", "TournamentMatch").objects.create(
            tournament=tournament,
            stage=stage,
            match_number=1,
            status="final",
            home_score=12,
            away_score=9,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        current = executor.loader.project_state(after).apps
        preserved = current.get_model("tournament", "TournamentMatch").objects.get(
            pk=match.pk
        )
        assert (preserved.home_score, preserved.away_score, preserved.status) == (
            12,
            9,
            "final",
        )
        assert preserved.cup_state == {}
        assert preserved.tournament.cup_rules is None
        assert not current.get_model("competition", "CupFixture").objects.exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
