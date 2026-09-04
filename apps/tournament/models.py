"""Persistent tournament planning and scoring models."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone as django_timezone


def default_tiebreakers() -> list[str]:
    """Return a fresh default standings rule list."""
    return ["points", "goal_difference", "goals_for", "head_to_head", "seed"]


class Tournament(models.Model):
    """An event-scoped korfball tournament."""

    class Status(models.TextChoices):
        """Tournament publication and operational states."""

        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        LIVE = "live", "Live"
        FINISHED = "finished", "Finished"
        ARCHIVED = "archived", "Archived"

    class Visibility(models.TextChoices):
        """Public listing policy."""

        PUBLIC = "public", "Public"
        UNLISTED = "unlisted", "Unlisted"

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    location = models.CharField(max_length=200, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/Amsterdam")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    visibility = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )
    display_token = models.UUIDField(default=uuidv7, editable=False, unique=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_korfbal_tournaments",
    )
    owner_id: int
    organizer_club = models.ForeignKey(
        "club.Club",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournaments",
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="TournamentMember",
        related_name="managed_korfbal_tournaments",
        blank=True,
    )
    win_points = models.SmallIntegerField(default=2)
    draw_points = models.SmallIntegerField(default=1)
    loss_points = models.SmallIntegerField(default=0)
    tiebreakers = models.JSONField(default=default_tiebreakers)
    match_duration_minutes = models.PositiveSmallIntegerField(
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(240)],
    )
    changeover_minutes = models.PositiveSmallIntegerField(
        default=5,
        validators=[MaxValueValidator(120)],
    )
    minimum_rest_minutes = models.PositiveSmallIntegerField(
        default=5,
        validators=[MaxValueValidator(240)],
    )
    live_revision = models.PositiveBigIntegerField(default=0)
    live_changed_at = models.DateTimeField(default=django_timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        fields: models.Manager[TournamentField]
        member_roles: models.Manager[TournamentMember]
        teams: models.Manager[TournamentTeam]
        stages: models.Manager[TournamentStage]
        final_groups: models.Manager[TournamentFinalGroup]
        pools: models.Manager[TournamentPool]
        matches: models.Manager[TournamentMatch]
        display_config: TournamentDisplayConfig

    class Meta:
        """Default ordering and common tournament lookup indexes."""

        ordering: ClassVar[list[str]] = ["-starts_at", "name"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["status", "starts_at"]),
            models.Index(fields=["owner", "starts_at"]),
        ]

    def __str__(self) -> str:
        """Return the event name."""
        return self.name


class TournamentField(models.Model):
    """A court or field that can host one tournament match at a time."""

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="fields",
    )
    label = models.CharField(max_length=80)
    sort_order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)

    if TYPE_CHECKING:
        assigned_pools: models.Manager[TournamentPool]
        matches: models.Manager[TournamentMatch]

    class Meta:
        """Keep labels unique and fields presentation-ordered."""

        ordering: ClassVar[list[str]] = ["sort_order", "label"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tournament", "label"],
                name="uniq_tournament_field_label",
            ),
        ]

    def __str__(self) -> str:
        """Return a tournament-qualified field label."""
        return f"{self.tournament.name} · {self.label}"


class TournamentMember(models.Model):
    """An organizer or field-scoped scorekeeper."""

    class Role(models.TextChoices):
        """Supported tournament collaboration roles."""

        MANAGER = "manager", "Manager"
        SCOREKEEPER = "scorekeeper", "Scorekeeper"

    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="member_roles",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="korfbal_tournament_roles",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    field = models.ForeignKey(
        TournamentField,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scorekeepers",
    )

    class Meta:
        """Allow one effective role per user and tournament."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tournament", "user"],
                name="uniq_tournament_member_user",
            ),
        ]

    def __str__(self) -> str:
        """Return a readable tournament membership label."""
        return f"{self.user} · {self.tournament} · {self.role}"


class TournamentTeam(models.Model):
    """A custom participant that may optionally reference an official team."""

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="teams",
    )
    tournament_id: UUID
    name = models.CharField(max_length=160)
    short_name = models.CharField(max_length=32, blank=True)
    affiliation = models.CharField(max_length=120, blank=True)
    linked_team = models.ForeignKey(
        "team.Team",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tournament_entries",
    )
    linked_team_id: UUID | None
    referee_access_token = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    seed = models.PositiveSmallIntegerField(default=1)
    sort_order = models.PositiveSmallIntegerField(default=0)
    color = models.CharField(max_length=16, blank=True)
    checked_in = models.BooleanField(default=False)
    withdrawn = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        pool_entries: models.Manager[TournamentPoolEntry]
        referee_matches: models.Manager[TournamentMatch]

    class Meta:
        """Keep custom team names unique within a tournament."""

        ordering: ClassVar[list[str]] = ["sort_order", "seed", "name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tournament", "name"],
                name="uniq_tournament_team_name",
            ),
        ]

    def __str__(self) -> str:
        """Return the custom team name."""
        return self.name


class TournamentFinalGroup(models.Model):
    """One independently qualified four-team bracket within a tournament."""

    class Format(models.TextChoices):
        """Supported qualification patterns for the first knockout round."""

        THREE_POOL_WILDCARD = "three_pool_wildcard", "Three pool winners + wildcard"
        TWO_POOL_CROSS = "two_pool_cross", "Two pool crossover"

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="final_groups",
    )
    tournament_id: UUID
    name = models.CharField(max_length=90)
    format = models.CharField(max_length=32, choices=Format.choices)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        stages: models.Manager[TournamentStage]

    class Meta:
        """Keep final-group names unique and presentation ordered."""

        ordering: ClassVar[list[str]] = ["sort_order", "name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tournament", "name"],
                name="uniq_tournament_final_group_name",
            ),
        ]

    def __str__(self) -> str:
        """Return a tournament-qualified final-group label."""
        return f"{self.tournament} · {self.name}"


class TournamentStage(models.Model):
    """An ordered pool, knockout, placement, or final phase."""

    class Kind(models.TextChoices):
        """Supported stage structures."""

        POOL = "pool", "Pool"
        KNOCKOUT = "knockout", "Knockout"
        PLACEMENT = "placement", "Placement"
        FINAL = "final", "Final"

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="stages",
    )
    final_group = models.ForeignKey(
        TournamentFinalGroup,
        on_delete=models.CASCADE,
        related_name="stages",
        null=True,
        blank=True,
    )
    final_group_id: UUID | None
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    sort_order = models.PositiveSmallIntegerField(default=0)
    qualifiers_per_pool = models.PositiveSmallIntegerField(default=0)

    class Meta:
        """Keep stage labels unique and explicitly ordered."""

        ordering: ClassVar[list[str]] = ["sort_order", "name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tournament", "name"],
                name="uniq_tournament_stage_name",
            ),
        ]

    def __str__(self) -> str:
        """Return a tournament-qualified stage label."""
        return f"{self.tournament} · {self.name}"


class TournamentPool(models.Model):
    """A named group within a pool stage."""

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="pools",
    )
    stage = models.ForeignKey(
        TournamentStage,
        on_delete=models.CASCADE,
        related_name="pools",
    )
    stage_id: UUID
    assigned_field = models.ForeignKey(
        TournamentField,
        on_delete=models.SET_NULL,
        related_name="assigned_pools",
        null=True,
        blank=True,
    )
    assigned_field_id: UUID | None
    name = models.CharField(max_length=80)
    sort_order = models.PositiveSmallIntegerField(default=0)
    teams = models.ManyToManyField(
        TournamentTeam,
        through="TournamentPoolEntry",
        related_name="pools",
    )

    if TYPE_CHECKING:
        entries: models.Manager[TournamentPoolEntry]
        matches: models.Manager[TournamentMatch]

    class Meta:
        """Keep pool labels unique within a tournament."""

        ordering: ClassVar[list[str]] = ["sort_order", "name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tournament", "name"],
                name="uniq_tournament_pool_name",
            ),
        ]

    def __str__(self) -> str:
        """Return a tournament-qualified pool label."""
        return f"{self.tournament} · {self.name}"


class TournamentPoolEntry(models.Model):
    """A seeded team membership in one pool."""

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    pool = models.ForeignKey(
        TournamentPool,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    pool_id: UUID | None
    team = models.ForeignKey(
        TournamentTeam,
        on_delete=models.CASCADE,
        related_name="pool_entries",
    )
    team_id: UUID
    seed_order = models.PositiveSmallIntegerField(default=1)

    if TYPE_CHECKING:
        adjustments: models.Manager[TournamentStandingAdjustment]

    class Meta:
        """Keep each team unique within one pool."""

        ordering: ClassVar[list[str]] = ["seed_order", "team__name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["pool", "team"],
                name="uniq_tournament_pool_team",
            ),
        ]

    def __str__(self) -> str:
        """Return a readable pool membership label."""
        return f"{self.pool} · {self.team}"


class TournamentMatch(models.Model):
    """A scheduled tournament match with a direct, auditable result."""

    class Status(models.TextChoices):
        """Tournament match lifecycle states."""

        SCHEDULED = "scheduled", "Scheduled"
        LIVE = "live", "Live"
        FINAL = "final", "Final"
        CANCELLED = "cancelled", "Cancelled"

    class DestinationSide(models.TextChoices):
        """Bracket destination slot choices."""

        HOME = "home", "Home"
        AWAY = "away", "Away"

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    tournament_id: UUID
    stage = models.ForeignKey(
        TournamentStage,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    stage_id: UUID
    pool = models.ForeignKey(
        TournamentPool,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matches",
    )
    pool_id: UUID | None
    home_team = models.ForeignKey(
        TournamentTeam,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="home_tournament_matches",
    )
    home_team_id: UUID | None
    away_team = models.ForeignKey(
        TournamentTeam,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="away_tournament_matches",
    )
    away_team_id: UUID | None
    home_qualifier = models.JSONField(default=dict, blank=True)
    away_qualifier = models.JSONField(default=dict, blank=True)
    field = models.ForeignKey(
        TournamentField,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matches",
    )
    field_id: UUID | None
    round_number = models.PositiveSmallIntegerField(default=1)
    match_number = models.PositiveIntegerField(default=1)
    starts_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveSmallIntegerField(default=20)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.SCHEDULED,
    )
    field_ready_at = models.DateTimeField(null=True, blank=True)
    field_ready_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="readied_tournament_matches",
    )
    field_ready_by_id: int | None
    field_ready_by_name = models.CharField(max_length=150, blank=True)
    referee_team = models.ForeignKey(
        TournamentTeam,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referee_matches",
    )
    referee_team_id: UUID | None
    referee_name = models.CharField(max_length=150, blank=True)
    referee_player = models.ForeignKey(
        "player.Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_tournament_referee_matches",
    )
    referee_player_id: UUID | None
    referee_access_token = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    referee_claim_token = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    referee_claimed_at = models.DateTimeField(null=True, blank=True)
    home_score = models.PositiveSmallIntegerField(null=True, blank=True)
    away_score = models.PositiveSmallIntegerField(null=True, blank=True)
    winner = models.ForeignKey(
        TournamentTeam,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="won_tournament_matches",
    )
    winner_id: UUID | None
    next_match = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_matches",
    )
    next_match_id: UUID | None
    winner_to_side = models.CharField(
        max_length=8,
        choices=DestinationSide.choices,
        blank=True,
    )
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        result_audits: models.Manager[TournamentResultAudit]

    class Meta:
        """Index live operations and protect match identity."""

        ordering: ClassVar[list[str]] = ["starts_at", "match_number"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tournament", "starts_at"]),
            models.Index(fields=["tournament", "status"]),
            models.Index(fields=["field", "starts_at"]),
            models.Index(fields=["pool", "status"]),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(home_team__isnull=True)
                | Q(away_team__isnull=True)
                | ~Q(home_team=models.F("away_team")),
                name="tournament_match_distinct_teams",
            ),
            models.UniqueConstraint(
                fields=["tournament", "match_number"],
                name="uniq_tournament_match_number",
            ),
        ]

    def __str__(self) -> str:
        """Return the match number and participant labels."""
        home = self.home_team.name if self.home_team else "TBD"
        away = self.away_team.name if self.away_team else "TBD"
        return f"#{self.match_number} {home} - {away}"


class TournamentStandingAdjustment(models.Model):
    """An audited bonus or penalty applied to a pool entry."""

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    entry = models.ForeignKey(
        TournamentPoolEntry,
        on_delete=models.CASCADE,
        related_name="adjustments",
    )
    points = models.SmallIntegerField()
    reason = models.CharField(max_length=240)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tournament_standing_adjustments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return the adjusted entry and signed point value."""
        return f"{self.entry}: {self.points:+d}"


class TournamentResultAudit(models.Model):
    """Append-only history for match result changes."""

    class Source(models.TextChoices):
        """Identify which scoring surface produced an audited change."""

        DIRECT = "direct", "Direct result edit"
        REFEREE_GOAL = "referee_goal", "Referee goal"
        REFEREE_UNDO = "referee_undo", "Referee goal removal"

    id_uuid = models.UUIDField(primary_key=True, default=uuidv7, editable=False)
    match = models.ForeignKey(
        TournamentMatch,
        on_delete=models.CASCADE,
        related_name="result_audits",
    )
    previous_home_score = models.PositiveSmallIntegerField(null=True, blank=True)
    previous_away_score = models.PositiveSmallIntegerField(null=True, blank=True)
    new_home_score = models.PositiveSmallIntegerField(null=True, blank=True)
    new_away_score = models.PositiveSmallIntegerField(null=True, blank=True)
    previous_status = models.CharField(max_length=16)
    new_status = models.CharField(max_length=16)
    reason = models.CharField(max_length=240, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tournament_result_changes",
    )
    changed_by_name = models.CharField(max_length=150, blank=True)
    source = models.CharField(
        max_length=24,
        choices=Source.choices,
        default=Source.DIRECT,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Show the most recent corrections first."""

        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self) -> str:
        """Return a compact audit description."""
        return f"{self.match} · {self.previous_status} → {self.new_status}"


class TournamentDisplayConfig(models.Model):
    """Presentation-screen configuration kept separate from event rules."""

    tournament = models.OneToOneField(
        Tournament,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="display_config",
    )
    rotation_seconds = models.PositiveSmallIntegerField(
        default=12,
        validators=[MinValueValidator(5), MaxValueValidator(120)],
    )
    show_live = models.BooleanField(default=True)
    show_standings = models.BooleanField(default=True)
    show_upcoming = models.BooleanField(default=True)
    show_recent = models.BooleanField(default=True)
    accent_color = models.CharField(max_length=16, default="#d6ff00")
    announcement = models.CharField(max_length=240, blank=True)

    def __str__(self) -> str:
        """Return the associated tournament label."""
        return f"Display · {self.tournament}"
