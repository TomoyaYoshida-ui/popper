"""Literal quotation binding shared by Scoop step5 and offline A3 fixtures.

A passage is bound only when, after whitespace normalization, it appears as a
consecutive substring of the supplied fulltext and is 20–300 characters long.
This is not an audit-grade scoop FPR product.
"""
from __future__ import annotations


def normalize_passage(text: str) -> str:
    return " ".join(str(text).split())


def passage_bound_to_fulltext(passage, content) -> bool:
    """True when passage is a 20–300 char consecutive quotation of content."""
    if not isinstance(passage, str) or not isinstance(content, str):
        return False
    stripped = passage.strip()
    if not (20 <= len(stripped) <= 300):
        return False
    return normalize_passage(stripped) in normalize_passage(content)
