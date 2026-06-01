"""Source-adapter interface (ADR-0002).

Each adapter harvests one portal via that portal's native incremental mechanism,
yields canonical OCDS 1.1.x releases, and knows how to build the public notice URL.
Adapter isolation (ADR-0004): one adapter failing must not affect the others.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any


class Adapter(ABC):
    #: canonical source key (config.SOURCE_*)
    key: str

    @abstractmethod
    def iter_releases(self, window_from: str, window_to: str) -> Iterator[dict[str, Any]]:
        """Yield OCDS releases updated within [window_from, window_to].

        Dates are 'YYYY-MM-DDTHH:MM:SS' (no offset). Implementations handle their
        own pagination and backoff and must be safely resumable per window.
        """
        raise NotImplementedError

    @abstractmethod
    def notice_url(self, release: dict[str, Any]) -> str:
        """Canonical public URL for the notice this release corresponds to."""
        raise NotImplementedError

    @abstractmethod
    def notice_type(self, release: dict[str, Any]) -> str | None:
        """Source notice/form code (UK4, F02, ...) if discoverable, for regime derivation."""
        raise NotImplementedError
