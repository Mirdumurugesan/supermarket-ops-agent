"""The reply the owner actually reads.

The model writes Markdown. Telegram renders nothing without a parse mode, so
the shop owner was seeing literal `**Bill Finalized**`. These tests pin the
conversion, and — more importantly — the cases where a naive conversion would
make Telegram reject the message outright and show him nothing at all.
"""

from __future__ import annotations

from kirana.telegram.formatting import (split_for_telegram, strip_tags,
                                        to_telegram_html)


def test_bold_becomes_bold_instead_of_asterisks():
    assert to_telegram_html("**Bill Finalized**") == "<b>Bill Finalized</b>"


def test_a_real_finalize_reply_renders_end_to_end():
    reply = (
        "✅ **Bill #INV-20260907-001 Finalized**\n\n"
        "- **Loose Sugar (2 kg)** @ ₹44 = ₹88.00\n"
        "- **Maggi Noodles (4 pcs)** @ ₹14 = ₹56.00\n\n"
        "**Total Amount:** ₹144.00 (UPI)"
    )
    out = to_telegram_html(reply)

    assert "**" not in out                       # the thing she reported
    assert "<b>Bill INV-20260907-001 Finalized</b>" in out
    assert "• <b>Loose Sugar (2 kg)</b> @ ₹44 = ₹88.00" in out
    assert "₹144.00" in out                      # rupees and paise survive


def test_the_invoice_number_is_not_a_hashtag():
    """Telegram links a leading # as a topic search. An invoice is not a topic."""
    assert "#" not in to_telegram_html("Bill #INV-20260907-001 done")


def test_markdown_that_would_break_telegrams_own_parsers_is_safe_here():
    """MarkdownV2 would 400 on every one of these; HTML does not care.

    This is the reason for the whole module: an unescaped `-` or `.` in
    MarkdownV2 means the owner sees no message at all, not an ugly one.
    """
    out = to_telegram_html("Loose Sugar (2 kg) - ₹88.00 [best price] "
                           "on 07-09-2026! 50% off #1")
    assert "(2 kg)" in out and "₹88.00" in out and "07-09-2026" in out
    assert "[best price]" in out


def test_html_special_characters_are_escaped_not_dropped():
    """Otherwise a stray < eats the rest of the message."""
    out = to_telegram_html("stock < 5 & reorder > 20")
    assert out == "stock &lt; 5 &amp; reorder &gt; 20"
    assert strip_tags(out) == "stock < 5 & reorder > 20"


def test_underscores_in_identifiers_are_left_alone():
    """`product_id` and @mirdu_kirana_bot must not turn into italics."""
    out = to_telegram_html("call add_product with product_id and reorder_level")
    assert "add_product" in out and "product_id" in out and "<i>" not in out


def test_code_spans_survive_and_their_contents_are_not_markup():
    out = to_telegram_html("Try `bill: 2kg sugar` or `**not bold**`")
    assert "<code>bill: 2kg sugar</code>" in out
    assert "<code>**not bold**</code>" in out     # inside code, markup is text


def test_bullets_become_bullets():
    assert to_telegram_html("- one\n- two") == "• one\n• two"


def test_messages_are_split_on_line_boundaries_not_mid_tag():
    """Splitting inside <b> would make Telegram reject the chunk."""
    line = "<b>Aashirvaad Atta 5kg</b> — 10 packets"
    chunks = split_for_telegram("\n".join([line] * 200), limit=400)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 400
        assert chunk.count("<b>") == chunk.count("</b>")


def test_a_short_message_is_not_split():
    assert split_for_telegram("₹144.00") == ["₹144.00"]


def test_strip_tags_is_a_readable_last_resort():
    """If Telegram ever rejects the markup, the owner still gets his total."""
    assert strip_tags("<b>Total:</b> ₹144.00") == "Total: ₹144.00"
