"""Canonicalisation and content hashing.

Content hash (PRD §7.4): a deterministic SHA-256 over canonicalised release JSON,
so re-crawls of an identical release are no-ops. Canonicalisation = JSON with sorted
keys, compact separators, after stripping volatile/ingest-meta fields. Versioned, so a
change to the recipe is a deliberate, reviewable event.

The index is a faithful, verbatim mirror of public-domain source data: releases are
reproduced as published by the source authorities (no content is altered or removed).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CANON_RECIPE_VERSION = "1"

# Fields stripped before hashing because they vary without the release meaningfully
# changing (re-export churn would otherwise mint a new hash and double-load).
_VOLATILE_TOP_KEYS = {"publishedDate"}


def canonical_json(obj: Any) -> str:
    """Stable serialisation: sorted keys, compact, UTF-8 safe."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(release: dict[str, Any]) -> str:
    """Deterministic idempotency key for a release."""
    stripped = {k: v for k, v in release.items() if k not in _VOLATILE_TOP_KEYS}
    payload = f"v{CANON_RECIPE_VERSION}:" + canonical_json(stripped)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
