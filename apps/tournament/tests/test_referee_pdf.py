"""PDF rendering coverage for printable tournament referee cards."""

from apps.tournament.services.referee_pdf import (
    RefereeDutyCard,
    build_referee_duties_pdf,
)


MIN_EXPECTED_PDF_BYTES = 5_000


def test_referee_pdf_renders_six_cards_per_a4_page() -> None:
    """A seventh referee duty starts a second printable page."""
    duties = [
        RefereeDutyCard(
            referee_team_name=f"Fluitend team {number}",
            access_url=f"https://korfbal.butrosgroot.com/tournaments/referee/token-{number}",
            match_number=number,
            home_team_name=f"Thuisteam {number}",
            away_team_name=f"Uitteam {number}",
            field_label=f"Veld {number}",
            starts_at_label="09:00",
        )
        for number in range(1, 8)
    ]

    document = build_referee_duties_pdf("Zomertoernooi", duties)

    assert document.startswith(b"%PDF-")
    assert document.rstrip().endswith(b"%%EOF")
    assert b"/Count 2" in document
    assert len(document) > MIN_EXPECTED_PDF_BYTES
