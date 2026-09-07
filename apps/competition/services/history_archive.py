"""Import explicitly attributed normalized archive results; never reconstruct scores."""

from copy import deepcopy
from datetime import timedelta
import re

from django.db import transaction
from django.utils import timezone

from apps.competition.models import Club, SyncLease
from apps.competition.services.history import seed, validate_match
from apps.competition.services.importer import Importer
from apps.schedule.models import Season


MAX_ARCHIVE_ROWS = 10000
MAX_SOURCE_ID_LENGTH = 80


def resolve_archive_rows(rows: list[dict]) -> tuple[list[dict], dict[str, Club]]:
    """Deduplicate archive rows and resolve canonical clubs in one read.

    Raises:
        ValueError: Duplicate matches conflict or a referenced club is unknown.

    """
    unique_rows = {}
    club_ids = set()
    for row in rows:
        identifier = str(row["PublicMatchId"])
        if identifier in unique_rows and row != unique_rows[identifier]:
            raise ValueError("Conflicting duplicate archive match")
        unique_rows[identifier] = row
        club_ids.update(
            str(row[side]["Club"]["ClubId"]) for side in ("HomeTeam", "AwayTeam")
        )
    clubs = {
        club.external_id: club for club in Club.objects.filter(external_id__in=club_ids)
    }
    if club_ids - clubs.keys():
        raise ValueError("Archive references an unknown club")
    return list(unique_rows.values()), clubs


@transaction.atomic
def import_archive(season: Season, document: dict) -> dict:
    """Import normalized archive fixtures with distinct IDs and attribution.

    Raises:
        ValueError: The supplied configuration or response is inconsistent.

    """
    # Archive ingestion also respects the global mutation boundary used by publication.
    lease, _ = SyncLease.objects.select_for_update().get_or_create(
        key="sportlink", defaults={"expires_at": timezone.now()}
    )
    if lease.owner is not None and lease.expires_at > timezone.now():
        raise ValueError("Importer lease is active")
    namespace = document["namespace"]
    if not re.fullmatch(r"[a-z0-9_-]{1,20}", namespace):
        raise ValueError("Invalid archive namespace")
    rows = document["matches"]
    if not isinstance(rows, list) or len(rows) > MAX_ARCHIVE_ROWS:
        raise ValueError("Archive batch exceeds 10000 rows")
    rows, clubs = resolve_archive_rows(rows)
    importer = Importer(season, timezone.now(), discover=False)
    count = 0
    for original in rows:
        row = deepcopy(original)
        original_id = str(row["PublicMatchId"])
        identifier = f"archive:{namespace}:{original_id}"
        resource = seed(
            season,
            "archive",
            "match",
            identifier,
            end=min(season.end_date, timezone.localdate() - timedelta(days=1)),
            reference=document["source"],
        )
        if resource.state == "fetched":
            continue
        row["PublicMatchId"] = identifier
        for side in ("HomeTeam", "AwayTeam"):
            team = row[side]
            team["PublicTeamId"] = f"archive:{namespace}:{team['PublicTeamId']}"
            if len(team["PublicTeamId"]) > MAX_SOURCE_ID_LENGTH:
                raise ValueError("Archive team ID too long")
            club = clubs[str(team["Club"]["ClubId"])]
            team["Club"] = {
                "ClubId": club.external_id,
                "ClubName": club.name,
                "City": club.city,
            }
        if row.get("Pool"):
            row["Pool"]["PoolId"] = f"archive:{namespace}:{row['Pool']['PoolId']}"
            if len(row["Pool"]["PoolId"]) > MAX_SOURCE_ID_LENGTH:
                raise ValueError("Archive pool ID too long")
        validate_match(row, resource)
        importer.match(row, result=True)
        resource.state, resource.coverage = "fetched", "partial"
        resource.reason = "archive_not_provider_verified"
        resource.evidence = {"original_match_id": original_id}
        resource.fetched_at = timezone.now()
        resource.save()
        count += 1
    return {
        "imported": count,
        "publication": "Run publish_competition to review/publish native links",
    }
