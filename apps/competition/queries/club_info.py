"""Public KNKV club information: website, founding date, venue, sports and kits."""

from typing import Any

from django.db.models import FETCH_RAISE

from apps.club.models.club import Club as NativeClub
from apps.competition.models import Club
from apps.competition.queries.match_info import kit


def _venue(facility: dict[str, str]) -> dict[str, Any] | None:
    """Use the match venue shape so both pages share their presentation."""
    if not facility.get("name") and not facility.get("address"):
        return None
    return {
        "name": facility.get("name"),
        "address": facility.get("address"),
        "postal_code": facility.get("postal_code"),
        "city": facility.get("city"),
        "phone": facility.get("phone"),
        "field": None,
        "surface": None,
        "visitor_info_url": None,
        "dressing_rooms": {"home": None, "away": None, "officials": None},
    }


def club_info(club: NativeClub) -> dict[str, Any]:
    """Return what KNKV publishes for the club; empty for unlinked clubs."""
    source = (
        Club.objects
        .filter(local_club=club)
        .only("contact", "sports", "colors")
        .fetch_mode(FETCH_RAISE)
        .first()
    )
    if source is None:
        return {
            "website": None,
            "founded": None,
            "venue": None,
            "activities": [],
            "kits": [],
        }
    contact = source.contact
    return {
        "website": contact.get("website"),
        "founded": contact.get("founded"),
        "venue": _venue(contact.get("facility") or {}),
        "activities": source.sports,
        "kits": kit(source),
    }
