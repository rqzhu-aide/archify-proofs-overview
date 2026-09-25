"""Normalize unusable PDF-extracted control codes, never authored mathematics."""
from __future__ import annotations


_REPLACEMENTS = {code: '\ufffd' for code in range(32) if chr(code) not in '\n\r\t'}


def sanitize_pdf_excerpt(text: str) -> tuple[str, str | None]:
    count = sum(ord(character) in _REPLACEMENTS for character in text)
    if not count:
        return text, None
    return text.translate(_REPLACEMENTS), (
        f"PDF extraction replaced {count} unsupported control character(s) with \ufffd (U+FFFD). "
        "Missing glyph meanings were not recovered; inspect the original page."
    )
