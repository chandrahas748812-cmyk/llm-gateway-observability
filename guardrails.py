"""Guardrail hooks: PII redaction, blocked-word policy, length limits.

These run on prompts BEFORE they reach a provider and on completions BEFORE
they are returned, so sensitive text never leaves the gateway unredacted.
Patterns are generic regexes for demo data, not a compliance product.
"""

from __future__ import annotations

import re

# name -> (pattern, replacement label)
PII_PATTERNS = {
    "email": (
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "[REDACTED:EMAIL]",
    ),
    "phone": (
        r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b",
        "[REDACTED:PHONE]",
    ),
    "ssn_like": (
        r"\b\d{3}-\d{2}-\d{4}\b",
        "[REDACTED:SSN]",
    ),
    "card_like": (
        r"\b(?:\d[ -]?){13,16}\b",
        "[REDACTED:CARD]",
    ),
}

_COMPILED = {
    name: re.compile(pattern) for name, (pattern, _) in PII_PATTERNS.items()
}


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Replace PII-like substrings. Returns (redacted_text, kinds_found)."""
    found: list[str] = []
    out = text
    for name, regex in _COMPILED.items():
        if regex.search(out):
            found.append(name)
            out = regex.sub(PII_PATTERNS[name][1], out)
    return out, found


def find_blocked(text: str, blocked_words: list[str]) -> list[str]:
    """Case-insensitive substring policy check. Returns matched words."""
    lowered = text.lower()
    return [w for w in blocked_words if w.lower() in lowered]


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate over-long text. Returns (text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
