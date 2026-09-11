"""Season-scoped Sportlink identities and reproducible competition results."""

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from django.db import models
from django.utils import timezone


class Club(models.Model):
    """Source club identity; never merge clubs by display name."""

    if TYPE_CHECKING:
        local_club_id: UUID | None

    external_id = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=255, blank=True)
    logo_bucket = models.CharField(max_length=80, blank=True)
    logo_hash = models.CharField(max_length=64, blank=True)
    cached_logo = models.CharField(max_length=1024, blank=True)
    published_logo = models.CharField(max_length=1024, blank=True)
    local_club = models.OneToOneField(
        "club.Club",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="competition_identity",
    )

    def __str__(self) -> str:
        """Return the source display name."""
        return self.name


class SeasonalIdentity(models.Model):
    """Prevent a reused upstream ID from overwriting an earlier season."""

    if TYPE_CHECKING:
        season_id: UUID

    season = models.ForeignKey("schedule.Season", on_delete=models.PROTECT)
    external_id = models.CharField(max_length=80)

    class Meta:
        """Declare storage or serialization metadata."""

        abstract = True
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("season", "external_id"), name="%(class)s_season_source"
            )
        ]

    def __str__(self) -> str:
        """Return the season-scoped source identity."""
        return str(self.external_id)


class TeamGroup(models.Model):
    """One club team per season, shared by indoor and outdoor source entries."""

    if TYPE_CHECKING:
        season_id: UUID
        local_team_id: UUID | None
        local_team_data_id: int | None

    season = models.ForeignKey("schedule.Season", on_delete=models.PROTECT)
    club = models.ForeignKey(Club, on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=765)
    local_team = models.ForeignKey(
        "team.Team",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="competition_groups",
    )

    local_team_data = models.OneToOneField(
        "team.TeamData",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="competition_identity",
    )

    class Meta:
        """Keep exact normalized names unique within a club and season."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("season", "club", "normalized_name"),
                name="competition_team_group_once",
            ),
            models.UniqueConstraint(
                fields=("season", "local_team"), name="competition_local_team_once"
            ),
        ]

    def __str__(self) -> str:
        """Return the shared team name."""
        return self.name


class SeasonBinding(models.Model):
    """Map an explicitly configured provider edition to a native playing season."""

    if TYPE_CHECKING:
        scope_id: UUID
        season_id: UUID

    scope = models.ForeignKey(
        "schedule.Season",
        on_delete=models.PROTECT,
        related_name="competition_season_bindings",
    )
    sport = models.CharField(max_length=80)
    season = models.ForeignKey("schedule.Season", on_delete=models.PROTECT)

    class Meta:
        """One target for each provider scope and discipline."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("scope", "sport"), name="competition_scope_sport_once"
            )
        ]

    def __str__(self) -> str:
        """Identify the mapping without loading its related seasons."""
        return f"{self.scope_id}:{self.sport}"


class Team(SeasonalIdentity):
    """Source team, including its indoor/outdoor sport identifier."""

    roster_observed_at = models.DateTimeField(null=True)
    private_roster_counts = models.JSONField(default=dict)

    local_team_data = models.ForeignKey(
        "team.TeamData",
        null=True,
        on_delete=models.PROTECT,
        related_name="competition_variants",
    )
    club = models.ForeignKey(Club, on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    sport = models.CharField(max_length=80, db_index=True)
    group = models.ForeignKey(
        TeamGroup, null=True, on_delete=models.PROTECT, related_name="variants"
    )

    if TYPE_CHECKING:
        group_id: int | None
        local_team_data_id: int | None

    def __str__(self) -> str:
        """Return the full team name."""
        return self.name


class CompetitionEdition(models.Model):
    """Season and playing context; unknown phases are not inferred from dates."""

    if TYPE_CHECKING:
        season_id: UUID

    season = models.ForeignKey("schedule.Season", on_delete=models.PROTECT)
    discipline = models.CharField(max_length=20)
    phase = models.CharField(max_length=20)
    gender = models.CharField(max_length=20)

    class Meta:
        """Keep context identities unique."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("season", "discipline", "phase", "gender"),
                name="competition_edition_context_once",
            )
        ]

    def __str__(self) -> str:
        """Return a recognizable source context."""
        return f"{self.season_id}:{self.discipline}:{self.phase}:{self.gender}"


class CompetitionClass(models.Model):
    """An official class within one edition, independent of poule numbering."""

    if TYPE_CHECKING:
        edition_id: int

    edition = models.ForeignKey(CompetitionEdition, on_delete=models.PROTECT)
    code = models.CharField(max_length=40)
    category = models.CharField(max_length=20)
    age_group = models.CharField(max_length=20)
    team_kind = models.CharField(max_length=20)
    colour = models.CharField(max_length=20)
    playing_format = models.CharField(max_length=20)
    level = models.PositiveSmallIntegerField(null=True)

    class Meta:
        """Keep context identities unique."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=(
                    "edition",
                    "code",
                    "category",
                    "age_group",
                    "team_kind",
                    "colour",
                    "playing_format",
                ),
                name="competition_class_context_once",
            )
        ]

    def __str__(self) -> str:
        """Return a recognizable source context."""
        return f"{self.edition_id}:{self.code}:{self.age_group}"


class CupCompetition(models.Model):
    """An observed season-specific cup, distinct from league classifications."""

    if TYPE_CHECKING:
        local_tournament_id: UUID | None

    season = models.ForeignKey("schedule.Season", on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    sport = models.CharField(max_length=80, blank=True)
    local_tournament = models.OneToOneField(
        "tournament.Tournament",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="source_cup",
    )

    class Meta:
        """Retain separate regional, discipline and season identities."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["season", "name", "sport"], name="unique_source_cup"
            ),
        ]

    def __str__(self) -> str:
        """Return the observed cup label."""
        return self.name


class CupFixture(models.Model):
    """Provider evidence and native tracking link for one cup fixture.

    Absent bracket metadata stays unknown; dates do not establish a round.
    """

    competition = models.ForeignKey(
        CupCompetition, on_delete=models.CASCADE, related_name="fixtures"
    )
    match = models.OneToOneField(
        "Match", on_delete=models.CASCADE, related_name="cup_fixture"
    )
    round_name = models.CharField(max_length=120, blank=True)
    round_number = models.PositiveSmallIntegerField(null=True, blank=True)
    next_fixture = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL
    )
    winner_to_side = models.CharField(
        max_length=4, blank=True, choices=[("home", "Home"), ("away", "Away")]
    )
    local_match = models.OneToOneField(
        "tournament.TournamentMatch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="source_cup_fixture",
    )

    def __str__(self) -> str:
        """Return the source fixture identity."""
        return str(self.match)


class Pool(SeasonalIdentity):
    """Poule metadata and freshness of its official standings."""

    name = models.CharField(max_length=255, blank=True)
    class_name = models.CharField(max_length=255, blank=True)
    sport = models.CharField(max_length=80, blank=True, db_index=True)
    if TYPE_CHECKING:
        competition_class_id: int | None
        entries: models.Manager["PoolEntry"]

    competition_class = models.ForeignKey(
        CompetitionClass,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pools",
    )
    mapping_status = models.CharField(max_length=20, default="unresolved")
    mapping_version = models.CharField(max_length=40, blank=True)
    mapping_issues = models.JSONField(default=list)
    mapping_override = models.JSONField(default=dict)
    mapping_evidence = models.JSONField(default=dict)
    standings_synced_at = models.DateTimeField(null=True)
    results_filtered = models.BooleanField(default=True)
    local_pool = models.OneToOneField(
        "schedule.SeasonPool",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="competition_identity",
    )

    def __str__(self) -> str:
        """Return a recognizable poule label."""
        return f"{self.class_name} {self.name}".strip() or self.external_id

    standing_rows: list["PoolEntry"]

    @property
    def member_teams(self) -> list[Team]:
        """Reuse prefetched memberships for bounded catalogue responses."""
        return [entry.team for entry in self.entries.all()]


class PoolEntry(models.Model):
    """Official standings, retaining source points and deductions."""

    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="entries")
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    standing = models.JSONField(default=dict)

    if TYPE_CHECKING:
        pool_id: int
        team_id: int

    class Meta:
        """Declare storage or serialization metadata."""

        constraints: ClassVar = [
            models.UniqueConstraint(fields=("pool", "team"), name="pool_team_once")
        ]

    def __str__(self) -> str:
        """Return the membership identity."""
        return f"{self.pool_id}:{self.team_id}"


class Match(SeasonalIdentity):
    """One fixture regardless of how many club/poule feeds include it."""

    if TYPE_CHECKING:
        pool_id: int | None
        home_team_id: int
        away_team_id: int
        local_match_id: UUID | None

    pool = models.ForeignKey(Pool, null=True, on_delete=models.PROTECT)
    home_team = models.ForeignKey(
        Team, on_delete=models.PROTECT, related_name="home_matches"
    )
    away_team = models.ForeignKey(
        Team, on_delete=models.PROTECT, related_name="away_matches"
    )
    starts_at = models.DateTimeField(db_index=True)
    lineup_observed_at = models.DateTimeField(null=True)
    private_lineup_counts = models.JSONField(default=dict)
    status = models.CharField(max_length=40, db_index=True)
    home_score = models.PositiveIntegerField(null=True)
    away_score = models.PositiveIntegerField(null=True)
    automatic_result = models.BooleanField(default=False)
    result_observed_at = models.DateTimeField(null=True)
    results_checked_at = models.DateTimeField(null=True)
    results_attempted_at = models.DateTimeField(null=True)
    missing_result_attempts = models.PositiveIntegerField(default=0)
    schedule_checked_at = models.DateTimeField(null=True)
    playing_time_minutes = models.PositiveSmallIntegerField(null=True)
    playing_time_observed_at = models.DateTimeField(null=True)
    match_periods = models.JSONField(default=list)
    facility_details = models.JSONField(default=dict)
    facility_observed_at = models.DateTimeField(null=True)
    match_rules = models.JSONField(default=dict)
    rules_observed_at = models.DateTimeField(null=True)
    reporting_delay_seconds = models.PositiveIntegerField(null=True)
    local_match = models.OneToOneField(
        "schedule.Match",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="competition_identity",
    )
    local_created = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    published_state = models.JSONField(default=dict, blank=True)
    published_schedule = models.JSONField(default=dict, blank=True)
    schedule_notification_id = models.UUIDField(null=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(SeasonalIdentity.Meta):
        """Index chronological season queries."""

        abstract = False
        indexes: ClassVar = [models.Index(fields=("season", "status", "starts_at"))]


class ResultRevision(models.Model):
    """Audit score corrections without retaining personal API payloads."""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="revisions")
    observed_at = models.DateTimeField()

    if TYPE_CHECKING:
        match_id: int

    status = models.CharField(max_length=40)
    home_score = models.PositiveIntegerField(null=True)
    away_score = models.PositiveIntegerField(null=True)
    automatic_result = models.BooleanField()

    def __str__(self) -> str:
        """Return the result observation identity."""
        return f"{self.match_id}@{self.observed_at}"


class SyncResource(models.Model):
    """Durable discovery queue and conditional-request checkpoint."""

    season = models.ForeignKey("schedule.Season", on_delete=models.CASCADE)
    kind = models.CharField(max_length=32)
    source_id = models.CharField(max_length=80, default="", blank=True)
    next_sync_at = models.DateTimeField(db_index=True)
    fetched_at = models.DateTimeField(null=True)
    etag = models.CharField(max_length=512, blank=True)
    # Exact membership of the last successful response, also used for HTTP 304.
    match_ids = models.JSONField(default=list)
    failures = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=80, blank=True)

    class Meta:
        """Declare storage or serialization metadata."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("season", "kind", "source_id"), name="competition_resource_once"
            )
        ]

    def __str__(self) -> str:
        """Return the resource identity."""
        return f"{self.kind}:{self.source_id}"


class SyncRun(models.Model):
    """Bounded, credential-free history of scheduled polling heartbeats."""

    season = models.ForeignKey("schedule.Season", on_delete=models.CASCADE)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=32, default="running")
    summary = models.JSONField(default=dict)
    backlog = models.JSONField(default=dict)
    diagnostics = models.JSONField(default=dict)
    heartbeat_at = models.DateTimeField(null=True)
    lease_owner = models.UUIDField(null=True)

    class Meta:
        """Keep recent season history cheap to read."""

        indexes: ClassVar = [models.Index(fields=("season", "started_at"))]

    def __str__(self) -> str:
        """Identify a heartbeat without provider or account data."""
        return f"{self.started_at:%Y-%m-%d %H:%M} · {self.status}"


class SyncLease(models.Model):
    """A global lease prevents concurrent importers exceeding the request pace."""

    key = models.CharField(max_length=32, primary_key=True)
    owner = models.UUIDField(null=True)
    expires_at = models.DateTimeField()

    def __str__(self) -> str:
        """Return the provider lease key."""
        return self.key


class TrafficState(models.Model):
    """Provider-wide request counters and spacing, durable across command runs."""

    key = models.CharField(max_length=32, primary_key=True)
    hour_start = models.DateTimeField()
    day_start = models.DateTimeField()
    rate_limited = models.BooleanField(default=False)
    hour_requests = models.PositiveIntegerField(default=0)
    day_requests = models.PositiveIntegerField(default=0)
    next_request_at = models.DateTimeField()

    def __str__(self) -> str:
        """Return the provider budget key."""
        return self.key


class HistoricalResource(models.Model):
    """Immutable discovery identity and resumable historical coverage checkpoint."""

    if TYPE_CHECKING:
        season_id: UUID

    season = models.ForeignKey("schedule.Season", on_delete=models.PROTECT)
    provider = models.CharField(max_length=20)
    kind = models.CharField(max_length=20)
    source_id = models.CharField(max_length=80)
    key = models.CharField(max_length=64, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    sport = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=20, default="pending", db_index=True)
    coverage = models.CharField(max_length=20, default="unknown")
    reason = models.CharField(max_length=80, blank=True)
    evidence = models.JSONField(default=dict)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    fetched_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    etag = models.CharField(max_length=512, blank=True)

    class Meta:
        """Index bounded workers by ready state and newest historical interval."""

        indexes: ClassVar = [models.Index(fields=("state", "next_attempt_at"))]
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="history_valid_date_interval",
            )
        ]

    def __str__(self) -> str:
        """Return a credential-free provider resource label."""
        return f"{self.provider}:{self.kind}:{self.source_id}"


class HistoricalDiscovery(models.Model):
    """Retain every discovery edge without duplicating upstream work."""

    resource = models.ForeignKey(
        HistoricalResource, on_delete=models.CASCADE, related_name="discoveries"
    )
    parent = models.ForeignKey(
        HistoricalResource, null=True, on_delete=models.PROTECT, related_name="children"
    )
    reference = models.CharField(max_length=512)

    class Meta:
        """Deduplicate provenance independently of the request checkpoint."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("resource", "reference"),
                name="history_discovery_once",
            )
        ]

    def __str__(self) -> str:
        """Return the attributed discovery reference."""
        return self.reference


class AllocationSource(models.Model):
    """Immutable allocation-file provenance; never store a machine-specific path."""

    season = models.ForeignKey("schedule.Season", on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    label = models.CharField(max_length=255)
    published_on = models.DateField()

    class Meta:
        """Keep context identities unique."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("season", "digest"), name="competition_allocation_source_once"
            )
        ]

    def __str__(self) -> str:
        """Return a recognizable source context."""
        return f"{self.label} ({self.published_on})"


class Allocation(models.Model):
    """A published team allocation, including aggregate age and original KNKV points."""

    if TYPE_CHECKING:
        entry_id: int | None
        competition_class_id: int | None

    competition_class = models.ForeignKey(
        CompetitionClass,
        null=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    source = models.ForeignKey(AllocationSource, on_delete=models.PROTECT)
    row_number = models.PositiveIntegerField()
    column = models.PositiveSmallIntegerField()
    section = models.CharField(max_length=255)
    pool_name = models.CharField(max_length=255)
    team_name = models.CharField(max_length=255)
    city = models.CharField(max_length=255)
    match_day = models.CharField(max_length=20)
    average_age = models.DecimalField(max_digits=4, decimal_places=1, null=True)
    knkv_points = models.DecimalField(max_digits=7, decimal_places=2, null=True)
    classification = models.JSONField(default=dict)
    entry = models.ForeignKey(
        PoolEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="allocations",
    )
    link_status = models.CharField(max_length=30, default="unmatched")

    class Meta:
        """Keep context identities unique."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("source", "row_number", "column"),
                name="competition_allocation_row_once",
            )
        ]

    def __str__(self) -> str:
        """Return a recognizable source context."""
        return f"{self.pool_name}: {self.team_name}"


class RosterMembership(models.Model):
    """Provider observation metadata for native players, not a second roster model."""

    player = models.ForeignKey(
        "player.Player", on_delete=models.CASCADE, related_name="knkv_memberships"
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="roster_memberships"
    )
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True)
    roles = models.JSONField(default=list)
    local_staff_link_created = models.BooleanField(default=False)
    local_coach_link_created = models.BooleanField(default=False)
    shirt_number = models.CharField(max_length=10, blank=True)
    published_team_data = models.ForeignKey(
        "team.TeamData", null=True, on_delete=models.SET_NULL
    )
    local_link_created = models.BooleanField(default=False)

    if TYPE_CHECKING:
        player_id: UUID
        team_id: int
        published_team_data_id: int | None

    class Meta:
        """Retain prior observations when a player leaves and later returns."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("player", "team"),
                condition=models.Q(ended_at__isnull=True),
                name="competition_active_roster_once",
            )
        ]

    def __str__(self) -> str:
        """Identify the source observation without loading player data."""
        return f"{self.player_id}:{self.team_id}"


class MatchMembership(models.Model):
    """A provider match-selection observation linking existing native people.

    Selection is not a tracked appearance, playing time or team-season membership.
    """

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="lineup")
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    player = models.ForeignKey("player.Player", on_delete=models.CASCADE)
    role = models.CharField(
        max_length=16,
        choices=[
            ("selected", "Selected, starting role unknown"),
            ("starter", "Starter"),
            ("substitute", "Substitute"),
            ("staff", "Staff"),
        ],
    )
    roles = models.JSONField(default=list)
    observed_at = models.DateTimeField()

    if TYPE_CHECKING:
        player_id: UUID
        team_id: int

    class Meta:
        """Keep one current selection per match and native person."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("match", "player"), name="competition_match_person_once"
            )
        ]

    def __str__(self) -> str:
        """Identify the observation without loading personal data."""
        return f"{self.pk}:{self.role}"


class RatingConfiguration(models.Model):
    """Explicit season opt-in to reviewed allocation baselines and Elo parameters."""

    season = models.OneToOneField("schedule.Season", on_delete=models.PROTECT)
    source_ids = models.JSONField(default=list)
    effective_at = models.DateTimeField()
    b_scale = models.FloatField()
    b_k_factor = models.FloatField()
    active = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        season_id: UUID

    def __str__(self) -> str:
        """Identify the configured season without loading its relation."""
        return f"{self.season_id}: allocation Elo"
