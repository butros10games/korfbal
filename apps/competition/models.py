"""Season-scoped Sportlink identities and reproducible competition results."""

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from django.db import models


class Club(models.Model):
    """Source club identity; never merge clubs by display name."""

    if TYPE_CHECKING:
        local_club_id: UUID | None

    external_id = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=255, blank=True)
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


class Team(SeasonalIdentity):
    """Source team, including its indoor/outdoor sport identifier."""

    club = models.ForeignKey(Club, on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    sport = models.CharField(max_length=80, db_index=True)
    group = models.ForeignKey(
        TeamGroup, null=True, on_delete=models.PROTECT, related_name="variants"
    )

    if TYPE_CHECKING:
        group_id: int | None

    def __str__(self) -> str:
        """Return the full team name."""
        return self.name


class Pool(SeasonalIdentity):
    """Poule metadata and freshness of its official standings."""

    name = models.CharField(max_length=255, blank=True)
    class_name = models.CharField(max_length=255, blank=True)
    sport = models.CharField(max_length=80, blank=True, db_index=True)
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
    status = models.CharField(max_length=40, db_index=True)
    home_score = models.PositiveIntegerField(null=True)
    away_score = models.PositiveIntegerField(null=True)
    automatic_result = models.BooleanField(default=False)
    result_observed_at = models.DateTimeField(null=True)
    results_checked_at = models.DateTimeField(null=True)
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
    hour_requests = models.PositiveIntegerField(default=0)
    day_requests = models.PositiveIntegerField(default=0)
    next_request_at = models.DateTimeField()

    def __str__(self) -> str:
        """Return the provider budget key."""
        return self.key
