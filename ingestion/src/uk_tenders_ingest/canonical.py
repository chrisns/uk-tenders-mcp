"""Canonicalisation, content hashing, and PII redaction.

Content hash (PRD §7.4): a deterministic SHA-256 over canonicalised release JSON,
so re-crawls of an identical release are no-ops. Canonicalisation = JSON with sorted
keys, compact separators, after stripping volatile/ingest-meta fields. Versioned, so a
change to the recipe is a deliberate, reviewable event.

PII redaction (PRD §10.1, ADR-0003): personal-data OCDS paths are stripped before any
row reaches the public dataset.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

CANON_RECIPE_VERSION = "1"

# Fields stripped before hashing because they vary without the release meaningfully
# changing (re-export churn would otherwise mint a new hash and double-load).
_VOLATILE_TOP_KEYS = {"publishedDate"}

# Personal-data fields removed from parties[].contactPoint before publication.
_PII_CONTACT_FIELDS = {"name", "email", "telephone", "faxNumber"}


def canonical_json(obj: Any) -> str:
    """Stable serialisation: sorted keys, compact, UTF-8 safe."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(release: dict[str, Any]) -> str:
    """Deterministic idempotency key for a release."""
    stripped = {k: v for k, v in release.items() if k not in _VOLATILE_TOP_KEYS}
    payload = f"v{CANON_RECIPE_VERSION}:" + canonical_json(stripped)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def redact(obj: Any) -> Any:
    """Return a deep copy with personal data removed.

    Removes parties[].contactPoint personal fields anywhere they appear in the
    OCDS structure. Conservative: walks the whole tree so nested/award/tender
    party copies are covered too.
    """
    clone = copy.deepcopy(obj)
    _redact_in_place(clone)
    return clone


def _redact_in_place(node: Any) -> None:
    if isinstance(node, dict):
        cp = node.get("contactPoint")
        if isinstance(cp, dict):
            for f in _PII_CONTACT_FIELDS:
                cp.pop(f, None)
        for v in node.values():
            _redact_in_place(v)
    elif isinstance(node, list):
        for item in node:
            _redact_in_place(item)
