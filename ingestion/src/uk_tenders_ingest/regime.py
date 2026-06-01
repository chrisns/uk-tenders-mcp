"""Regime derivation (PRD §6).

No source flags the regime; we derive it from the notice/form code and date.
  pca2023  — Procurement Act 2023 notices (UK1–UK17), live from 2025-02-24
  legacy   — PCR2015 etc. (TED-style F/T forms)
  scotland — Scottish processes (own regulations)
  mixed    — a process whose releases span regimes (assigned at process level)
Regime is assigned per release; the process-level value is `mixed` if releases differ.
"""

from __future__ import annotations

import re

PCA2023_CUTOVER = "2025-02-24"

_UK_NOTICE_RE = re.compile(r"^UK([0-9]{1,2})$", re.IGNORECASE)
_LEGACY_FORM_RE = re.compile(r"^[FT][0-9]{2}$", re.IGNORECASE)


def regime_for_release(notice_type: str | None, publication_date: str | None, source: str) -> str:
    """Best-effort regime for a single release.

    notice_type: the source notice/form code if known (UK1..UK17, F01.., T01..).
    publication_date: ISO date/datetime string.
    source: canonical source key (Scotland → 'scotland').
    """
    if source == "pcs":
        return "scotland"
    if notice_type:
        if _UK_NOTICE_RE.match(notice_type):
            return "pca2023"
        if _LEGACY_FORM_RE.match(notice_type):
            return "legacy"
    # Fall back to the cutover date when the code is unknown.
    if publication_date and publication_date[:10] >= PCA2023_CUTOVER:
        return "pca2023"
    return "legacy"


def process_regime(release_regimes: list[str]) -> str:
    distinct = {r for r in release_regimes if r}
    if not distinct:
        return "legacy"
    if len(distinct) == 1:
        return next(iter(distinct))
    # scotland is its own track; otherwise spanning regimes → mixed
    if distinct == {"scotland"}:
        return "scotland"
    return "mixed"
