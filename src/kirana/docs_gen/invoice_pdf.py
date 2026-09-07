"""GST-correct PDF invoice, generated with ReportLab.

Layout follows a standard Indian retail tax invoice: seller block with GSTIN,
invoice number/date, per-line HSN + qty + rate + taxable + CGST + SGST,
slab-wise tax summary, round-off line and amount in words. Shop identity comes
from durable preferences, so "set shop name to X" changes future invoices.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from ..services import memory_service
from ..services.billing_service import get_bill

IST = ZoneInfo("Asia/Kolkata")
OUT_DIR = Path("data/artifacts")

ACCENT = colors.HexColor("#5b2333")
LIGHT = colors.HexColor("#f4eeee")

# The built-in Helvetica has no ₹ glyph; register DejaVu Sans when available
# (installed via fonts-dejavu-core in the Dockerfile) and fall back to "Rs.".
_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
RUPEE = "Rs."
for _cand_reg, _cand_bold in [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]:
    if Path(_cand_reg).exists() and Path(_cand_bold).exists():
        pdfmetrics.registerFont(TTFont("DVS", _cand_reg))
        pdfmetrics.registerFont(TTFont("DVS-Bold", _cand_bold))
        _FONT, _FONT_BOLD, RUPEE = "DVS", "DVS-Bold", "₹"
        break


def _amount_in_words(n: int) -> str:
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
            "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def two(x):
        return ones[x] if x < 20 else (tens[x // 10] + (" " + ones[x % 10] if x % 10 else ""))

    def three(x):
        return (ones[x // 100] + " Hundred" + (" " + two(x % 100) if x % 100 else "")) if x >= 100 else two(x)

    if n == 0:
        return "Zero"
    parts = []
    for div, label in ((10000000, "Crore"), (100000, "Lakh"), (1000, "Thousand")):
        if n >= div:
            parts.append(three(n // div) + " " + label)
            n %= div
    if n:
        parts.append(three(n))
    return " ".join(parts)


def generate_invoice_pdf(bill_id: int) -> str:
    bill = get_bill(bill_id)
    if bill["status"] != "finalized":
        raise ValueError(f"Bill #{bill_id} is {bill['status']} — only finalized bills get invoices.")

    prefs = memory_service.get_preferences()
    shop = prefs.get("shop_name", "Kirana Store")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{bill['invoice_no']}.pdf"

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=16, textColor=ACCENT,
                        spaceAfter=2)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5,
                           textColor=colors.HexColor("#555555"))
    normal = styles["Normal"]
    for st in (h1, small, normal):
        st.fontName = _FONT

    doc = SimpleDocTemplate(str(path), pagesize=A4, topMargin=14 * mm,
                            bottomMargin=14 * mm, leftMargin=14 * mm, rightMargin=14 * mm)
    story = []

    story.append(Paragraph(shop, h1))
    addr_bits = [prefs.get("shop_address"), prefs.get("shop_phone")]
    story.append(Paragraph(" · ".join(b for b in addr_bits if b), small))
    if prefs.get("gstin"):
        story.append(Paragraph(f"GSTIN: {prefs['gstin']}", small))
    story.append(Spacer(1, 4 * mm))

    finalized_utc = datetime.fromisoformat(bill["finalized_at"]).replace(tzinfo=ZoneInfo("UTC"))
    dt = finalized_utc.astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    meta = [
        ["TAX INVOICE", ""],
        [f"Invoice No: {bill['invoice_no']}", f"Date: {dt}"],
        [f"Payment: {bill['payment_mode'].upper()}"
         + (f" ({bill['payment_ref']})" if bill['payment_ref'] else ""),
         f"Customer: {bill.get('khata_customer') or bill.get('customer_name') or 'Walk-in'}"],
    ]
    t = Table(meta, colWidths=[95 * mm, 87 * mm])
    t.setStyle(TableStyle([
        ("SPAN", (0, 0), (1, 0)),
        ("FONTNAME", (0, 0), (1, 0), _FONT_BOLD),
        ("TEXTCOLOR", (0, 0), (1, 0), ACCENT),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 1), (-1, -1), _FONT),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(t)
    story.append(Spacer(1, 3 * mm))

    head = ["#", "Item", "HSN", "Qty", f"Rate {RUPEE}", f"Taxable {RUPEE}", f"CGST {RUPEE}", f"SGST {RUPEE}", f"Total {RUPEE}"]
    rows = [head]
    for i, it in enumerate(bill["items"], 1):
        rows.append([
            str(i), f"{it['name']}", it["hsn"], f"{it['qty']:g} {it['unit']}",
            f"{it['unit_price']:.2f}", f"{it['taxable_value']:.2f}",
            f"{it['cgst']:.2f}", f"{it['sgst']:.2f}", f"{it['line_total']:.2f}",
        ])
    items_t = Table(rows, colWidths=[8 * mm, 52 * mm, 14 * mm, 20 * mm, 18 * mm,
                                     22 * mm, 16 * mm, 16 * mm, 20 * mm], repeatRows=1)
    items_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 1), (-1, -1), _FONT),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(items_t)
    story.append(Spacer(1, 3 * mm))

    # slab-wise tax summary
    slab_map: dict = {}
    for it in bill["items"]:
        s = slab_map.setdefault(it["gst_rate"], {"taxable": 0.0, "cgst": 0.0, "sgst": 0.0})
        s["taxable"] += it["taxable_value"]
        s["cgst"] += it["cgst"]
        s["sgst"] += it["sgst"]
    slab_rows = [["GST Slab", f"Taxable {RUPEE}", f"CGST {RUPEE}", f"SGST {RUPEE}", f"Tax {RUPEE}"]]
    for rate in sorted(slab_map):
        s = slab_map[rate]
        slab_rows.append([f"{rate:g}%", f"{s['taxable']:.2f}", f"{s['cgst']:.2f}",
                          f"{s['sgst']:.2f}", f"{s['cgst'] + s['sgst']:.2f}"])
    slab_t = Table(slab_rows, colWidths=[19 * mm, 24 * mm, 21 * mm, 21 * mm, 21 * mm], hAlign="LEFT")
    slab_t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 1), (-1, -1), _FONT),
        ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
    ]))

    totals_rows = [
        ["Subtotal (taxable)", f"{RUPEE} {bill['subtotal']:.2f}"],
        ["CGST + SGST", f"{RUPEE} {bill['tax_total']:.2f}"],
        ["Round-off", f"{RUPEE} {bill['round_off']:+.2f}"],
        ["GRAND TOTAL", f"{RUPEE} {bill['grand_total']:.2f}"],
    ]
    totals_t = Table(totals_rows, colWidths=[42 * mm, 28 * mm], hAlign="RIGHT")
    totals_t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), _FONT_BOLD),
        ("TEXTCOLOR", (0, -1), (-1, -1), ACCENT),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    wrap = Table([[slab_t, totals_t]], colWidths=[108 * mm, 74 * mm])
    wrap.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(wrap)
    story.append(Spacer(1, 4 * mm))

    story.append(Paragraph(
        f"Amount in words: Rupees {_amount_in_words(int(round(bill['grand_total'])))} Only",
        normal))
    if bill["payment_mode"] == "khata":
        story.append(Paragraph(
            f"<b>On khata</b> — added to {bill.get('khata_customer')}'s ledger.", normal))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("Thank you, visit again!", small))

    doc.build(story)
    return str(path)
