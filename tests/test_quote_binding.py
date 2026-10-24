"""Offline quote-binding rules shared with Scoop step5. No network."""
from popper.quote_binding import normalize_passage, passage_bound_to_fulltext


CONTENT = (
    "This methodological passage explains the core mechanism in detail "
    "and continues with enough padding for length checks."
)


def test_normalize_collapses_whitespace():
    assert normalize_passage("a \n\t b") == "a b"


def test_passage_too_short_fails():
    assert not passage_bound_to_fulltext("short", CONTENT)
    assert not passage_bound_to_fulltext("x" * 19, CONTENT)


def test_passage_exactly_twenty_chars_bound():
    span = "core mechanism in de"
    assert len(span.strip()) == 20
    assert span in CONTENT
    assert passage_bound_to_fulltext(span, CONTENT)


def test_paraphrase_not_bound():
    assert not passage_bound_to_fulltext(
        "This methodological section describes the main method carefully!!",
        CONTENT)


def test_whitespace_variant_still_bound():
    span = CONTENT[5:45]
    spaced = "  ".join(span.split())
    assert passage_bound_to_fulltext(spaced, CONTENT)


def test_non_string_rejected():
    assert not passage_bound_to_fulltext(None, CONTENT)
    assert not passage_bound_to_fulltext(CONTENT[:25], None)
    assert not passage_bound_to_fulltext(12, CONTENT)
