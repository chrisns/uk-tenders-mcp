# Launch with all five sources (no phased public rollout)

**Status:** accepted (product-owner decision, overriding the phased-rollout recommendation)

Against the usual advice to ship the richest single source first and add the rest incrementally, the product will go public **only once all five portals** (FTS, Contracts Finder, PCS, Sell2Wales, eTendersNI) are ingested — to deliver a genuine "all UK tenders" index from day one.

The principal risk is **eTendersNI**: it publishes no OCDS and no public API, so it requires HTML scraping (or a third-party feed), and a fragile scraper could otherwise hold the entire launch hostage. Mitigation is **strict adapter isolation**: each source's adapter runs and fails independently; a **source-health** surface reports per-source freshness/status; the index **degrades gracefully** (a down or stale source is flagged, never fatal to the others).

## Considered options

- **All sources before launch (chosen)** — complete vision on day one; honest "all UK tenders" claim.
- **Phased, FTS first** — recommended but rejected by the product owner; would have shipped value sooner and de-risked the scraper.
- **OCDS-native first, scraped later** — rejected for the same reason.

## Consequences

Longer time-to-first-release. The eTendersNI scraper is the critical-path item — before committing to brittle HTML scraping, probe the EPPS platform for an undocumented JSON/RSS endpoint (as Ireland's etenders.gov.ie exposes) and evaluate a third-party aggregator (OpenOpps/bidstats) as a cleaner NI source. Internally, the build still sequences FTS → Contracts Finder → PCS → Sell2Wales → eTendersNI so the canonical model and the cross-source matcher are proven on OCDS-native sources before the hard one.
