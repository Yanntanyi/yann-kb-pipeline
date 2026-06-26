"""Deterministic date extraction for documents.

Phase 1 originally relied entirely on the LLM to find a document's date, and only
passed it the body text — never the filename. That missed:
  - every CR whose date lives only in the filename (e.g. "20250213 - Zenminio.md"),
  - body dates the model skipped when several dates appeared "in passing".

This module finds a date with plain rules. Priority (see resolve_date):
  1. the filename (YYYYMMDD / YYYY-MM-DD …) — the most reliable CR authoring date,
  2. an existing LLM-extracted date (kept, never clobbered),
  3. an explicitly labelled body line ("Date:", "Incident Date:" …), then the
     first well-formed date anywhere in the body.

Returns an ISO "YYYY-MM-DD" string, or None. No external dependencies.
"""

from __future__ import annotations

import re
from typing import Optional

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
          r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")

# 20250213 / 2025-02-13 / 2026.01.16 — anywhere in a filename
_FILENAME_DATE = re.compile(r"(20\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])")
# Body forms
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTH_DMY = re.compile(_MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.I)
_DMY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+(20\d{2})", re.I)
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b")  # US M/D/Y
# A line that explicitly labels a date we should trust over a date-in-passing.
_LABELLED = re.compile(
    r"(?:^|\n)\s*[*_# ]*"
    r"(?:incident\s+resolution\s+date|incident\s+date|report\s+date|document\s+date|"
    r"date\s+of\s+rca|change\s+date|date)\s*[:\-]\s*([^\n|]+)",
    re.I,
)


def _valid(y: int, m: int, d: int) -> Optional[str]:
    if 2000 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return None


def parse_date_string(s: str) -> Optional[str]:
    """Parse the first date expression found in `s` into ISO YYYY-MM-DD, or None."""
    m = _ISO.search(s)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _MONTH_DMY.search(s)
    if m:
        return _valid(int(m.group(3)), _MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)))
    m = _DMY_MONTH.search(s)
    if m:
        return _valid(int(m.group(3)), _MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1)))
    m = _SLASH.search(s)
    if m:
        yr = int(m.group(3))
        yr = yr + 2000 if yr < 100 else yr
        return _valid(yr, int(m.group(1)), int(m.group(2)))  # US month/day
    return None


def date_from_filename(filename: str) -> Optional[str]:
    m = _FILENAME_DATE.search(filename)
    return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def date_from_body(content: str) -> Optional[str]:
    """Prefer an explicitly labelled date line; else the first date in the text."""
    for m in _LABELLED.finditer(content):
        iso = parse_date_string(m.group(1))
        if iso:
            return iso
    return parse_date_string(content)


def resolve_date(filename: str, content: str, llm_date: Optional[str] = None) -> Optional[str]:
    """Conservative merge used by both Phase 1 and the staging patch.

    Filename date wins (most reliable). Otherwise keep an existing LLM date rather
    than risk overwriting it with a date-in-passing. Only when both are absent do
    we fall back to a body date. So this can ADD coverage but never regress a date
    the LLM already got right.
    """
    fn = date_from_filename(filename)
    if fn:
        return fn
    if llm_date:
        return llm_date
    return date_from_body(content)
