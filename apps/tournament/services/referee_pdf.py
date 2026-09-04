"""Printable PDF cards for account-free tournament referee duties."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas


PAGE_MARGIN = 8 * mm
CARD_GAP = 4 * mm
CARD_COLUMNS = 2
CARD_ROWS = 3
CARDS_PER_PAGE = CARD_COLUMNS * CARD_ROWS
CARD_WIDTH = (A4[0] - (2 * PAGE_MARGIN) - CARD_GAP) / CARD_COLUMNS
CARD_HEIGHT = (A4[1] - (2 * PAGE_MARGIN) - (2 * CARD_GAP)) / CARD_ROWS
QR_SIZE = 48 * mm
TEXT_PADDING = 6 * mm


@dataclass(frozen=True, slots=True)
class RefereeDutyCard:
    """All printable information for one match's referee handout."""

    referee_team_name: str
    access_url: str
    match_number: int
    home_team_name: str
    away_team_name: str
    field_label: str
    starts_at_label: str


def _fit_text(text: str, *, font: str, size: float, max_width: float) -> str:
    """Shorten one line without letting it leave its printable card."""
    normalized = " ".join(text.split())
    if pdfmetrics.stringWidth(normalized, font, size) <= max_width:
        return normalized

    suffix = "..."
    candidate = normalized
    while (
        candidate
        and pdfmetrics.stringWidth(f"{candidate}{suffix}", font, size) > max_width
    ):
        candidate = candidate[:-1]
    return f"{candidate.rstrip()}{suffix}"


def _draw_qr(pdf: Canvas, access_url: str, *, x: float, y: float) -> None:
    """Draw a sharp vector QR suitable for office printers."""
    qr = QrCodeWidget(access_url)
    left, bottom, right, top = qr.getBounds()
    width = right - left
    height = top - bottom
    drawing = Drawing(
        QR_SIZE,
        QR_SIZE,
        transform=[QR_SIZE / width, 0, 0, QR_SIZE / height, 0, 0],
    )
    drawing.add(qr)
    renderPDF.draw(drawing, pdf, x, y)


def _draw_card(
    pdf: Canvas,
    duty: RefereeDutyCard,
    *,
    tournament_name: str,
    x: float,
    y: float,
) -> None:
    """Render one cut-out referee card."""
    center_x = x + (CARD_WIDTH / 2)
    text_width = CARD_WIDTH - (2 * TEXT_PADDING)
    top = y + CARD_HEIGHT

    def draw_text(
        text: str,
        baseline: float,
        font: str,
        size: float,
        color: colors.Color = colors.black,
    ) -> None:
        pdf.setFillColor(color)
        pdf.setFont(font, size)
        pdf.drawCentredString(
            center_x,
            baseline,
            _fit_text(text, font=font, size=size, max_width=text_width),
        )

    pdf.setStrokeColor(colors.HexColor("#9CA3AF"))
    pdf.setLineWidth(0.6)
    pdf.setDash(3, 2)
    pdf.roundRect(x, y, CARD_WIDTH, CARD_HEIGHT, 5, stroke=1, fill=0)
    pdf.setDash()

    draw_text(
        "FLUITEND TEAM",
        top - (7 * mm),
        "Helvetica-Bold",
        7,
        colors.HexColor("#4B5563"),
    )
    draw_text(
        duty.referee_team_name,
        top - (13 * mm),
        "Helvetica-Bold",
        13,
    )

    _draw_qr(
        pdf,
        duty.access_url,
        x=center_x - (QR_SIZE / 2),
        y=y + (23 * mm),
    )

    draw_text(
        f"{duty.home_team_name} - {duty.away_team_name}",
        y + (17 * mm),
        "Helvetica-Bold",
        9.5,
    )
    draw_text(
        duty.field_label,
        y + (12 * mm),
        "Helvetica-Bold",
        10,
    )
    draw_text(
        f"Wedstrijd {duty.match_number} · {duty.starts_at_label}",
        y + (7 * mm),
        "Helvetica",
        7.5,
        colors.HexColor("#4B5563"),
    )
    draw_text(
        tournament_name,
        y + (3 * mm),
        "Helvetica",
        6,
        colors.HexColor("#6B7280"),
    )


def build_referee_duties_pdf(
    tournament_name: str,
    duties: Sequence[RefereeDutyCard],
) -> bytes:
    """Return an A4 PDF containing six cut-out referee cards per page."""
    output = BytesIO()
    pdf = Canvas(output, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"Scheidsrechter QR-codes - {tournament_name}")
    pdf.setSubject("Printbare directe QR-codes voor alle fluitbeurten")

    for index, duty in enumerate(duties):
        position = index % CARDS_PER_PAGE
        if index > 0 and position == 0:
            pdf.showPage()
        column = position % CARD_COLUMNS
        row = position // CARD_COLUMNS
        x = PAGE_MARGIN + (column * (CARD_WIDTH + CARD_GAP))
        y = A4[1] - PAGE_MARGIN - CARD_HEIGHT - (row * (CARD_HEIGHT + CARD_GAP))
        _draw_card(pdf, duty, tournament_name=tournament_name, x=x, y=y)

    pdf.save()
    return output.getvalue()
