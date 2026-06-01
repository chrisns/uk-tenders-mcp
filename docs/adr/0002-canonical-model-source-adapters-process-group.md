# Canonical OCDS model, per-source adapters, and a synthetic cross-source process identity

**Status:** accepted

The index aggregates five UK portals — **Find a Tender (FTS)**, **Contracts Finder**, **Public Contracts Scotland**, **Sell2Wales**, **eTendersNI**. Each source is ingested by its **own adapter** that normalises into a single **canonical OCDS 1.1.x model**; publisher-specific schema versions, field mappings, and extensions are absorbed at the adapter boundary, never leaked downstream.

An **OCID is publisher-scoped**: each portal mints its own prefix (`ocds-h6vhtk` FTS, `ocds-b5fd17` Contracts Finder, `ocds-r6ebe6` PCS, `ocds-kuma6s` Sell2Wales), and the *same* real procurement is legally published on several of them. Therefore **OCID is used only for within-source compilation and is never a cross-source key.** Cross-source identity is a derived, probabilistic **process group**: blocking on (buyer, CPV, value band, month), then fuzzy scoring on (normalised buyer, title, value+currency, key dates, CPV, any notice reference), exposed as a synthetic internal id alongside the raw OCIDs and a match confidence.

## Considered options

- **FTS-only** — rejected: the product owner wants genuine all-UK coverage, including Scottish below-threshold (excluded from FTS) and pre-2025 England below-threshold (only on Contracts Finder).
- **Per-source silos, no canonical model** — rejected: breaks cross-source analytics and a single query surface.
- **OCID as a universal key** — rejected: publisher-scoped prefixes mean OCIDs neither collide *nor* unify across sources, so it would both miss duplicates and fragment processes.

## Consequences

Duplicates are treated as the **norm**, not an error (above-threshold notices are cross-posted by law; PCS and Sell2Wales auto-forward to FTS). Net-new devolved volume is small once deduplicated.

The matcher needs a **measured evaluation loop**, because public dedup thresholds (Spend Network, Stotles, Tussell) are not disclosed and there is no authoritative cross-source key to check against. The plan: build a gold set from *blocked* candidate pairs (random pairs are trivially non-matching), hand-label a stratified sample including near-misses, and seed/validate with documented auto-forward pairs (PCS/Sell2Wales→FTS, NI→FTS — known-true by construction, usable as a **recall proxy** since absolute recall is unknowable). Set explicit launch thresholds (e.g. precision ≥ 0.9 on merges) and state which error is favoured. **Neither default is neutral:** `stitch=off` double-counts cross-posted notices (inflated totals); `stitch=on` can mis-merge two real procurements (deflated/mis-attributed) — so aggregate responses must surface the active mode and a confidence note. `relatedProcesses` links and any future stable CDP organisation identifier are bonus signals, not the backbone.

See [ADR-0004](0004-launch-with-all-five-sources.md) for the decision to ship all sources at once.
