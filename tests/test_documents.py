"""Artifacts: the PDF invoice and the PPTX analysis deck are really generated."""

from __future__ import annotations

from pathlib import Path

import pytest

from kirana.docs_gen.analysis_pptx import generate_analysis_deck
from kirana.docs_gen.invoice_pdf import generate_invoice_pdf
from kirana.services import analytics_service as analytics
from kirana.services import billing_service as billing

CHAT = 999


def _finalized_bill(key="doc-1"):
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 1, 4)       # 12% slab
    billing.add_item(b["id"], 2, 2)       # 5% slab
    billing.add_item(b["id"], 4, 1)       # 0% slab — all three on one invoice
    billing.set_payment(b["id"], "upi", reference="UPI/9911")
    return billing.finalize_bill(b["id"], key)


def test_invoice_pdf_is_generated_and_non_trivial(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bill = _finalized_bill()
    path = Path(generate_invoice_pdf(bill["id"]))
    assert path.exists()
    assert path.suffix == ".pdf"
    assert path.stat().st_size > 2000                 # a real document, not a stub
    assert path.read_bytes().startswith(b"%PDF")
    assert bill["invoice_no"] in path.name


def test_draft_bills_get_no_invoice(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 1, 1)
    with pytest.raises(ValueError):
        generate_invoice_pdf(b["id"])


def test_analysis_deck_is_generated_with_slides(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _finalized_bill("doc-2")
    today = analytics.today_ist()
    path = Path(generate_analysis_deck(today, today))
    assert path.exists() and path.suffix == ".pptx"

    from pptx import Presentation
    prs = Presentation(str(path))
    assert len(prs.slides) >= 6                        # title + KPIs + charts + insights
    # at least one embedded chart image (real charts, not text)
    pictures = [sh for slide in prs.slides for sh in slide.shapes if sh.shape_type == 13]
    assert len(pictures) >= 3


def test_analysis_deck_handles_an_empty_period(db, tmp_path, monkeypatch):
    """No sales in range must not crash the deck generator."""
    monkeypatch.chdir(tmp_path)
    path = Path(generate_analysis_deck("2020-01-01", "2020-01-07"))
    assert path.exists()
