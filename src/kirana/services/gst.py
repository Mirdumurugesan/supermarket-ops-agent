"""GST computation engine.

Rules implemented (kirana retail, intra-state B2C):

* Selling prices are **GST-inclusive** (MRP-style), the norm in Indian retail.
* Each SKU carries its own HSN code and tax slab — slabs are *data*, not code,
  so a GST-council rate change is a one-row UPDATE, not a deploy.
* Intra-state supply → tax splits equally into CGST + SGST.
* Per line: taxable value = line_total / (1 + rate); tax = line_total − taxable.
  CGST is rounded half-up to 2dp, SGST takes the remainder so
  taxable + CGST + SGST == line_total to the paisa, always.
* Invoice total is rounded to the nearest rupee with an explicit round-off
  line (standard Indian invoice practice, Section 170 CGST Act).

All math uses Decimal — float rounding errors on money are an automatic fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

TWO_DP = Decimal("0.01")
RUPEE = Decimal("1")


def d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def money(x) -> Decimal:
    return d(x).quantize(TWO_DP, rounding=ROUND_HALF_UP)


@dataclass
class TaxLine:
    """One bill line with its full tax breakup."""

    name: str
    hsn: str
    qty: Decimal
    unit: str
    unit_price: Decimal          # GST-inclusive
    gst_rate: Decimal            # percent
    line_total: Decimal = field(default=Decimal("0"))
    taxable_value: Decimal = field(default=Decimal("0"))
    cgst: Decimal = field(default=Decimal("0"))
    sgst: Decimal = field(default=Decimal("0"))


def compute_line(name: str, hsn: str, qty, unit: str, unit_price, gst_rate) -> TaxLine:
    qty, unit_price, gst_rate = d(qty), d(unit_price), d(gst_rate)
    line_total = money(qty * unit_price)
    divisor = Decimal("1") + gst_rate / Decimal("100")
    taxable = money(line_total / divisor)
    tax = line_total - taxable
    cgst = money(tax / 2)
    sgst = tax - cgst                      # remainder → breakup always reconciles
    return TaxLine(name, hsn, qty, unit, unit_price, gst_rate, line_total, taxable, cgst, sgst)


@dataclass
class BillTotals:
    subtotal: Decimal            # sum of taxable values
    cgst_total: Decimal
    sgst_total: Decimal
    tax_total: Decimal
    gross: Decimal               # sum of line totals (pre round-off)
    round_off: Decimal
    grand_total: Decimal         # payable, whole rupees
    slab_summary: dict           # {rate: {"taxable":…, "cgst":…, "sgst":…}}


def compute_totals(lines: list[TaxLine]) -> BillTotals:
    subtotal = sum((l.taxable_value for l in lines), Decimal("0"))
    cgst_total = sum((l.cgst for l in lines), Decimal("0"))
    sgst_total = sum((l.sgst for l in lines), Decimal("0"))
    gross = sum((l.line_total for l in lines), Decimal("0"))
    grand = gross.quantize(RUPEE, rounding=ROUND_HALF_UP)
    round_off = money(grand - gross)

    slabs: dict = {}
    for l in lines:
        s = slabs.setdefault(str(l.gst_rate), {"taxable": Decimal("0"), "cgst": Decimal("0"), "sgst": Decimal("0")})
        s["taxable"] += l.taxable_value
        s["cgst"] += l.cgst
        s["sgst"] += l.sgst

    return BillTotals(subtotal, cgst_total, sgst_total, cgst_total + sgst_total,
                      gross, round_off, grand, slabs)
