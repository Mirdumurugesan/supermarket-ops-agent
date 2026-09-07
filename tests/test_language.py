"""Which language the owner gets his bill in.

The prompt used to say "reply in the language they used" and the model read a
Hindi product alias as licence to answer entirely in Hindi — then drifted into
Marathi mid-reply, on a bill typed in plain English. Asking a small model to
judge this does not hold, so the decision moved into code: the reply follows the
script the owner actually typed in.
"""

from __future__ import annotations

import pytest

from kirana.agent.system_prompt import (build_system_prompt, current_language,
                                        detect_reply_language)


@pytest.mark.parametrize("message", [
    "bill: 2kg sugar, 4 maggi, UPI",
    "how much surf excel is left?",
    "50 packets of Maggi came in, cost 12, MRP 14",
    "put 500 on Ramesh's credit",
])
def test_plain_english_gets_english(message):
    assert detect_reply_language(message) == "English"


@pytest.mark.parametrize("message", [
    "bill: 2kg sakkarai, 4 maggi, UPI",       # Tamil word, English sentence
    "2kg atta and 1 paruppu",                 # Hindi + Tamil words
    "settle Ramesh's khata",
])
def test_indian_retail_words_are_not_a_language_switch(message):
    """The actual bug: `sakkarai` in an English sentence produced a Hindi bill."""
    assert detect_reply_language(message) == "English"


def test_hindi_script_gets_hindi():
    assert detect_reply_language("2 किलो चीनी का बिल बनाओ") == "Hindi"


def test_tamil_script_gets_tamil():
    assert detect_reply_language("2 கிலோ சர்க்கரை பில் போடு") == "Tamil"


def test_a_mixed_message_follows_the_dominant_script():
    """Real messages mix — "4 maggi चाहिए" is a Hindi message with a brand in it."""
    assert detect_reply_language("मुझे 4 maggi चाहिए") == "Hindi"


def test_digits_and_symbols_alone_do_not_pick_a_language():
    assert detect_reply_language("₹500 4 2kg") == "English"
    assert detect_reply_language("") == "English"


def test_the_instruction_actually_reaches_the_prompt(db):
    """A rule the model never sees is not a rule."""
    token = current_language.set("Tamil")
    try:
        assert "WRITE YOUR ENTIRE REPLY IN TAMIL" in build_system_prompt()
    finally:
        current_language.reset(token)

    assert "WRITE YOUR ENTIRE REPLY IN ENGLISH" in build_system_prompt()
