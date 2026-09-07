"""GST engine correctness — the tax maths a reviewer will check by hand."""

from __future__ import annotations

from decimal import Decimal

from kirana.services.gst import compute_line, compute_totals


def test_line_breakup_reconciles_exactly():
    """taxable + CGST + SGST must equal the line total to the paisa."""
    line = compute_line("Maggi", "1902", 4, "packet", 14, 12)
    assert line.line_total == Decimal("56.00")
    assert line.taxable_value + line.cgst + line.sgst == line.line_total


def test_inclusive_price_backs_out_tax_correctly():
    # ₹56 inclusive at 12% → taxable 56/1.12 = 50.00, tax 6.00, split 3.00/3.00
    line = compute_line("Maggi", "1902", 4, "packet", 14, 12)
    assert line.taxable_value == Decimal("50.00")
    assert line.cgst == Decimal("3.00")
    assert line.sgst == Decimal("3.00")


def test_zero_rated_staple_has_no_tax():
    line = compute_line("Rice", "1006", 2, "kg", 62, 0)
    assert line.cgst == Decimal("0.00") and line.sgst == Decimal("0.00")
    assert line.taxable_value == line.line_total == Decimal("124.00")


def test_odd_paisa_split_never_loses_a_paisa():
    """A tax amount that doesn't halve evenly goes to SGST as the remainder."""
    line = compute_line("Odd", "9999", 1, "piece", Decimal("10.05"), 5)
    assert line.taxable_value + line.cgst + line.sgst == line.line_total
    assert abs(line.cgst - line.sgst) <= Decimal("0.01")


def test_fractional_quantity_for_loose_items():
    line = compute_line("Sugar", "1701", Decimal("2.5"), "kg", 44, 5)
    assert line.line_total == Decimal("110.00")
    assert line.taxable_value + line.cgst + line.sgst == line.line_total


def test_totals_round_to_rupee_with_explicit_round_off():
    lines = [
        compute_line("Maggi", "1902", 4, "packet", 14, 12),        # 56.00
        compute_line("Sugar", "1701", Decimal("2.5"), "kg", 44, 5),  # 110.00
        compute_line("Odd", "9999", 1, "piece", Decimal("10.40"), 5),  # 10.40
    ]
    t = compute_totals(lines)
    assert t.gross == Decimal("176.40")
    assert t.grand_total == Decimal("176")
    assert t.round_off == Decimal("-0.40")
    assert t.grand_total == t.gross + t.round_off


def test_totals_split_by_slab():
    lines = [
        compute_line("Maggi", "1902", 4, "packet", 14, 12),
        compute_line("Sugar", "1701", 2, "kg", 44, 5),
        compute_line("Rice", "1006", 1, "kg", 62, 0),
    ]
    t = compute_totals(lines)
    assert set(t.slab_summary) == {"12", "5", "0"}
    assert t.slab_summary["0"]["cgst"] == Decimal("0.00")
    # every slab's parts sum back to the overall tax
    total_from_slabs = sum(s["cgst"] + s["sgst"] for s in t.slab_summary.values())
    assert total_from_slabs == t.tax_total


def test_cgst_equals_sgst_in_aggregate_within_a_paisa_per_line():
    lines = [compute_line(f"i{i}", "1902", 1, "piece", Decimal("13.33"), 18) for i in range(10)]
    t = compute_totals(lines)
    assert abs(t.cgst_total - t.sgst_total) <= Decimal("0.10")
    assert t.subtotal + t.tax_total == t.gross
