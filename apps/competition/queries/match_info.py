"""Public venue, rules and kit information for a native match from its KNKV source."""

from typing import Any

from django.db.models import FETCH_RAISE

from apps.competition.models import Club, Match
from apps.schedule.models import Match as NativeMatch


OUTFIT_KINDS = {"HomeOutfit": "home", "ReserveOutfit": "reserve", "AwayOutfit": "away"}


def _text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return (value.strip() or None) if isinstance(value, str) else None


def _venue(data: dict[str, Any]) -> dict[str, Any] | None:
    """Read MatchFacility; assembly and departure times are team logistics."""
    venue = {
        "name": _text(data, "FacilityName"),
        "address": _text(data, "Address"),
        "postal_code": _text(data, "ZipCode"),
        "city": _text(data, "City"),
        "phone": _text(data, "PhoneNumber"),
        "field": _text(data, "SubFacilityName"),
        "surface": _text(data, "FieldType"),
        "visitor_info_url": _text(data, "ClubInformationUrl"),
        "dressing_rooms": {
            "home": _text(data, "HomeDressingRoom"),
            "away": _text(data, "AwayDressingRoom"),
            "officials": _text(data, "OfficialDressingRoom"),
        },
    }
    return venue if venue["name"] or venue["address"] else None


def _sections(rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep the provider's labelled categories; ClassExtraAttributes stay internal."""
    sections = []
    for category in rules.get("Category") or []:
        if not isinstance(category, dict):
            continue
        items = [
            {"label": label, "value": value}
            for info in category.get("Info") or []
            if isinstance(info, dict)
            and (label := _text(info, "Key"))
            and (value := _text(info, "Value"))
        ]
        title = _text(category, "Name")
        if title and items:
            sections.append({"title": title.capitalize(), "items": items})
    return sections


def kit(club: Club) -> list[dict[str, Any]]:
    """Return the club's filled outfits in home, reserve, away order."""
    outfits = []
    for key, kind in OUTFIT_KINDS.items():
        parts = club.colors.get(key) or {}
        if parts:
            outfits.append({
                "kind": kind,
                "shirt": parts.get("Shirt"),
                "shorts": parts.get("Shorts"),
                "socks": parts.get("Stocking"),
            })
    return outfits


def match_info(match: NativeMatch) -> dict[str, Any]:
    """Return what KNKV publishes for the fixture; empty for unlinked matches."""
    source = (
        Match.objects
        .filter(local_match=match)
        .select_related("home_team__club", "away_team__club")
        .only(
            "facility_details",
            "match_rules",
            "home_team__club__colors",
            "away_team__club__colors",
        )
        .fetch_mode(FETCH_RAISE)
        .first()
    )
    if source is None:
        return {"venue": None, "sections": [], "kits": {"home": [], "away": []}}
    return {
        "venue": _venue(source.facility_details),
        "sections": _sections(source.match_rules),
        "kits": {
            "home": kit(source.home_team.club),
            "away": kit(source.away_team.club),
        },
    }
