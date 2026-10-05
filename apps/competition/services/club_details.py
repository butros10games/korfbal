"""Identity-bound public metadata from the club-level app endpoints."""

from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.competition.models import Club
from apps.competition.services.logos import discover_logo
from apps.schedule.models import Season


FACILITY_FIELDS = {
    "FacilityId": "id",
    "FacilityName": "name",
    "Address": "address",
    "ZipCode": "postal_code",
    "City": "city",
    "PhoneNumber": "phone",
}
OUTFITS = ("HomeOutfit", "AwayOutfit", "ReserveOutfit")
OUTFIT_PARTS = ("Shirt", "Shorts", "Stocking")
MAX_SPORT_DEFINITIONS = 100


def _text(data: dict[str, Any], key: str, *, limit: int = 1024) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"Invalid club metadata {key}")
    return value.strip()


def club_colors(data: object) -> dict[str, dict[str, str]]:
    """Keep only bounded public outfit strings; ignore unverified extra keys.

    Raises:
        TypeError: An outfit or the collection has an invalid shape.

    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TypeError("Invalid club metadata ClubColors")
    colors: dict[str, dict[str, str]] = {}
    for outfit in OUTFITS:
        parts = data.get(outfit)
        if parts is None:
            continue
        if not isinstance(parts, dict):
            raise TypeError("Invalid club metadata outfit")
        filled = {
            part: value
            for part in OUTFIT_PARTS
            if (value := _text(parts, part, limit=255))
        }
        if filled:
            colors[outfit] = filled
    return colors


def catalogue_values(
    data: dict[str, Any], *, replace_colors: bool = False
) -> dict[str, Any]:
    """Validate public catalogue fields without treating blank rows as deletion.

    Raises:
        ValueError: A supplied dissolution status is invalid.

    """
    values: dict[str, Any] = {}
    for source, target in (("ClubName", "name"), ("City", "city")):
        if value := _text(data, source, limit=255):
            values[target] = value
    if "Dissolved" in data and data["Dissolved"] is not None:
        if not isinstance(data["Dissolved"], bool):
            raise ValueError("Invalid club metadata Dissolved")
        if data["Dissolved"]:
            values["dissolved"] = True
    if data.get("ClubColors") is not None:
        colors = club_colors(data["ClubColors"])
        if colors or replace_colors:
            values["colors"] = colors
    return values


def update_club_metadata(
    club: Club,
    values: dict[str, Any],
    observed_at: datetime,
    *,
    fill_only: bool = False,
) -> None:
    """Apply field observations monotonically, preserving historical precedence."""
    observations = dict(club.metadata_observations)
    changed: list[str] = []
    for field, value in values.items():
        stamp = parse_datetime(observations.get(field, ""))
        if stamp is not None and not timezone.is_naive(stamp) and stamp > observed_at:
            continue
        # Historical evidence can establish dissolution even after a named
        # directory observation; ordinary false/absent values never reactivate.
        if fill_only and field != "dissolved" and getattr(club, field):
            continue
        changed_value = getattr(club, field) != value
        if changed_value:
            setattr(club, field, value)
            changed.append(field)
        if observations.get(field) != observed_at.isoformat():
            observations[field] = observed_at.isoformat()
    if observations != club.metadata_observations:
        club.metadata_observations = observations
        changed.append("metadata_observations")
    if changed:
        club.save(update_fields=changed)


def _club(source_id: str, data: dict[str, Any]) -> Club:
    """Lock only the requested club after validating the returned identity.

    Raises:
        ValueError: The requested club is unknown or the response identity differs.

    """
    if data.get("ClubId") != source_id:
        raise ValueError("Unrecognized club details")
    club = (
        Club.objects
        .select_for_update(no_key=True)
        .filter(external_id=source_id)
        .first()
    )
    if club is None:
        raise ValueError("Unknown club")
    return club


@transaction.atomic
def import_club_contact(
    source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Keep public website, founding date and main venue, excluding own contacts.

    Raises:
        ValueError: The facility payload has an invalid shape.

    """
    club = _club(source_id, data)
    facility = data.get("Facility")
    if facility is not None and not isinstance(facility, dict):
        raise ValueError("Invalid club metadata Facility")
    venue = {
        field: value
        for key, field in FACILITY_FIELDS.items()
        if (value := _text(facility or {}, key))
    }
    contact: dict[str, object] = {
        key: value
        for key, value in {
            "website": _text(data, "Website"),
            "founded": _text(data, "Founded"),
        }.items()
        if value
    }
    if venue:
        contact["facility"] = venue
    update_club_metadata(club, {"contact": contact}, observed_at)


@transaction.atomic
def import_club_sports(
    source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Retain sport identities and the existing ordered activities projection.

    Raises:
        TypeError: A sport row has an invalid shape.
        ValueError: The sport collection is missing or exceeds its bound.

    """
    club = _club(source_id, data)
    sports = data.get("ClubSport")
    if not isinstance(sports, list) or len(sports) > MAX_SPORT_DEFINITIONS:
        raise ValueError("Missing or invalid club sports")
    definitions: dict[tuple[str, str], dict[str, str]] = {}
    for sport in sports:
        if not isinstance(sport, dict):
            raise TypeError("Invalid club metadata sport")
        source = _text(sport, "SportId", limit=80)
        description = _text(sport, "SportDescription", limit=255)
        if source or description:
            definitions[source, description] = {
                "id": source,
                "description": description,
            }
    update_club_metadata(
        club,
        {
            "sports": list(
                dict.fromkeys(
                    row["description"]
                    for row in definitions.values()
                    if row["description"]
                )
            ),
            "sport_definitions": list(definitions.values()),
        },
        observed_at,
    )


@transaction.atomic
def import_club_details(
    season: Season, source_id: str, data: dict[str, Any], observed_at: datetime
) -> None:
    """Import Club v1 public fields without initiating competition discovery."""
    club = _club(source_id, data)
    update_club_metadata(club, catalogue_values(data, replace_colors=True), observed_at)
    discover_logo(club, data.get("ClubLogo"), season, observed_at=observed_at)
