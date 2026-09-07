"""Arguments arrive the way the owner talks, not the way the schema wishes.

Live bug, found in a demo run: the owner typed "put rs.500 on Ramesh's credit",
the model faithfully passed `amount="rs.500"`, the service compared a string to
an int, and the turn died with "internal error". Two layers now stop that — the
boundary normalises money-shaped strings, and a tool that still raises comes
back as a tool result the model can act on instead of killing the turn.
"""

from __future__ import annotations

import pytest

from kirana.agent.tools import coerce_arguments

MONEY = {"properties": {"amount": {"type": "number"},
                        "qty": {"type": "integer"},
                        "customer": {"type": "string"}}}


@pytest.mark.parametrize("written,expected", [
    ("rs.500", 500.0),
    ("Rs 500", 500.0),
    ("₹500", 500.0),
    ("₹1,200.50", 1200.5),
    ("500", 500.0),
    ("INR 250", 250.0),
])
def test_money_as_a_shopkeeper_writes_it_becomes_a_number(written, expected):
    assert coerce_arguments(MONEY, {"amount": written})["amount"] == expected


def test_integers_stay_integers():
    out = coerce_arguments(MONEY, {"qty": "6"})
    assert out["qty"] == 6 and isinstance(out["qty"], int)


def test_numbers_and_strings_are_left_alone():
    args = {"amount": 500.0, "qty": 4, "customer": "Ramesh"}
    assert coerce_arguments(MONEY, args) == args


def test_a_customer_named_like_a_number_is_not_coerced():
    """Only fields the schema calls numeric are touched."""
    assert coerce_arguments(MONEY, {"customer": "500"})["customer"] == "500"


def test_genuine_nonsense_is_passed_through_to_fail_loudly():
    """Silently turning "later" into 0 would put a wrong number in the books."""
    assert coerce_arguments(MONEY, {"amount": "later"})["amount"] == "later"


def test_unknown_fields_survive():
    assert coerce_arguments(MONEY, {"note": "half kg"})["note"] == "half kg"
