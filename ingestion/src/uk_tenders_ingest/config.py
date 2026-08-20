"""Configuration for the ingestion pipeline.

Everything is overridable by environment variable so the same code runs locally,
in a Cloud Run Job, and in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Canonical source keys (also the `source` column value everywhere).
SOURCE_FTS = "fts"
SOURCE_CONTRACTS_FINDER = "contracts_finder"
SOURCE_PCS = "pcs"
SOURCE_SELL2WALES = "sell2wales"
SOURCE_ETENDERSNI = "etendersni"

ALL_SOURCES = [
    SOURCE_FTS,
    SOURCE_CONTRACTS_FINDER,
    SOURCE_PCS,
    SOURCE_SELL2WALES,
    SOURCE_ETENDERSNI,
]


@dataclass(frozen=True)
class SourceConfig:
    key: str
    base_url: str
    # Earliest data available for backfill (per-source floor, PRD §7.4).
    backfill_floor: str  # ISO date
    # Public website notice-URL template; {id} is the source notice id.
    notice_url_template: str


SOURCES: dict[str, SourceConfig] = {
    SOURCE_FTS: SourceConfig(
        key=SOURCE_FTS,
        base_url="https://www.find-tender.service.gov.uk/api/1.0",
        backfill_floor="2021-01-01",
        notice_url_template="https://www.find-tender.service.gov.uk/Notice/{id}",
    ),
    # The remaining adapters are stubbed pending live verification (PRD §12);
    # their configs are recorded so the matrix is complete and wiring is ready.
    SOURCE_CONTRACTS_FINDER: SourceConfig(
        key=SOURCE_CONTRACTS_FINDER,
        base_url="https://www.contractsfinder.service.gov.uk",
        backfill_floor="2016-11-01",
        notice_url_template="https://www.contractsfinder.service.gov.uk/Notice/{id}",
    ),
    SOURCE_PCS: SourceConfig(
        key=SOURCE_PCS,
        base_url="https://api.publiccontractsscotland.gov.uk/v1",
        backfill_floor="2019-01-01",
        notice_url_template="https://www.publiccontractsscotland.gov.uk/NoticeDownload/Notice.aspx?id={id}",
    ),
    SOURCE_SELL2WALES: SourceConfig(
        key=SOURCE_SELL2WALES,
        base_url="https://api.sell2wales.gov.wales/v1",
        backfill_floor="2016-11-01",
        notice_url_template="https://www.sell2wales.gov.wales/search/show/search_view.aspx?ID={id}",
    ),
    SOURCE_ETENDERSNI: SourceConfig(
        key=SOURCE_ETENDERSNI,
        base_url="https://etendersni.gov.uk/epps",
        backfill_floor="2016-01-01",
        notice_url_template="https://etendersni.gov.uk/epps/cft/prepareViewCfTWS.do?resourceId={id}",
    ),
}


@dataclass(frozen=True)
class Settings:
    project: str
    raw_dataset: str
    public_dataset: str
    bq_location: str
    # safety lookback applied to nightly watermark (PRD §7.2)
    nightly_lookback_minutes: int
    # default backfill window size in days (windowed replay, PRD §7.4)
    window_days: int
    http_timeout_s: int
    max_retries: int

    @staticmethod
    def from_env() -> Settings:
        return Settings(
            project=os.environ.get("GCP_PROJECT", "uk-tenders-mcp"),
            raw_dataset=os.environ.get("BQ_RAW_DATASET", "uk_tenders_raw"),
            public_dataset=os.environ.get("BQ_PUBLIC_DATASET", "uk_tenders_public"),
            bq_location=os.environ.get("BQ_LOCATION", "EU"),
            nightly_lookback_minutes=int(os.environ.get("NIGHTLY_LOOKBACK_MIN", "120")),
            window_days=int(os.environ.get("WINDOW_DAYS", "7")),
            http_timeout_s=int(os.environ.get("HTTP_TIMEOUT_S", "30")),
            max_retries=int(os.environ.get("HTTP_MAX_RETRIES", "8")),
        )
