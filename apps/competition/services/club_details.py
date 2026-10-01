"""Club contact details and sports from the club-level app endpoints."""

from typing import Any

from apps.competition.models import Club


FACILITY_FIELDS = {
    "FacilityName": "name",
    "Address": "address",
    "ZipCode": "postal_code",
    "City": "city",
    "PhoneNumber": "phone",
}


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _store(source_id: str, data: dict[str, Any], **values: object) -> None:
    """Bind the response to the requested club and update only that row.

    Raises:
        ValueError: The response names another club or the club is unknown.

    """
    if str(data.get("ClubId", source_id)) != source_id:
        raise ValueError("Unrecognized club details")
    if not Club.objects.filter(external_id=source_id).update(**values):
        raise ValueError("Unknown club")


def import_club_contact(source_id: str, data: dict[str, Any]) -> None:
    """Keep the public website, founding date and main venue.

    The club's own phone number and email are often a volunteer's personal
    contact details, so they are never stored.
    """
    facility = data.get("Facility")
    venue = (
        {
            field: value
            for key, field in FACILITY_FIELDS.items()
            if (value := _text(facility, key))
        }
        if isinstance(facility, dict)
        else {}
    )
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
    _store(source_id, data, contact=contact)


def import_club_sports(source_id: str, data: dict[str, Any]) -> None:
    """Keep the descriptions in provider order, without duplicates.

    Raises:
        ValueError: The response has no sport list.

    """
    match data.get("ClubSport"):
        case list() as sports:
            descriptions = [
                description
                for sport in sports
                if isinstance(sport, dict)
                and (description := _text(sport, "SportDescription"))
            ]
        case _:
            raise ValueError("Missing club sports")
    _store(source_id, data, sports=list(dict.fromkeys(descriptions)))
