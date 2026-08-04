"""The sample questions offered on an empty chat.

They are the first thing a visitor clicks, so they are held to the same standard
as the tool layer: every one must be answerable from the loaded export. A
suggested question the dataset cannot answer produces a correct refusal that
reads, to someone seeing the product for the first time, like a broken demo.
"""

from __future__ import annotations

from finchat.data.store import GOLD_TABLES
from finchat.ui.chat import SAMPLE_QUESTIONS


def test_there_are_some_and_they_are_unique() -> None:
    assert len(SAMPLE_QUESTIONS) >= 3
    assert len(set(SAMPLE_QUESTIONS)) == len(SAMPLE_QUESTIONS)


def test_each_is_a_question_a_person_would_type() -> None:
    for q in SAMPLE_QUESTIONS:
        assert q.endswith("?"), q
        assert 15 < len(q) < 90, q  # long enough to be specific, short on a chip


def test_none_names_a_company_outside_the_dataset(store) -> None:
    """Guards the failure this feature could most easily cause.

    Tesla is the canonical out-of-scope example in the acceptance script: asking
    for it is *supposed* to be refused. Offering it as a suggestion would make
    the refusal look like breakage rather than integrity.
    """
    known = {r["company_name"].lower() for r in store.q("SELECT company_name FROM company_profile")}
    for q in SAMPLE_QUESTIONS:
        for word in ("tesla", "microsoft", "nvidia", "amazon"):
            if word in q.lower():
                assert any(word in name for name in known), f"{q!r} names {word}, not in the data"


def test_they_exercise_more_than_one_table() -> None:
    """A set of four questions that all hit one table demos one query path."""
    text = " ".join(SAMPLE_QUESTIONS).lower()
    assert "restatement" in text  # restatement_event
    assert "compan" in text  # company_profile
    assert len(GOLD_TABLES) == 4  # the export shape these were written against
