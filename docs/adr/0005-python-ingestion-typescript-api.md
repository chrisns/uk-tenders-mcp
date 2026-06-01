# Python ingestion, TypeScript MCP API

**Status:** accepted (supersedes the all-TypeScript intent for the ingestion tier)

Ingestion — source adapters, OCDS up-conversion/validation, record compilation (merge), and cross-source matching — is written in **Python**; the MCP read API is **TypeScript**. This matches `govreposcrape`'s *actual* split (its ingestion is Python — `container/ingest.py` — and only its API is TypeScript), which an earlier all-TypeScript decision had assumed away.

## Why

The canonical OCDS compile/merge is the Python `ocds-merge`/`ocdskit` library, whose rules (`omitWhenMerged`, `wholeListMerge`, identifier-keyed array merge, nulls-delete/later-wins) are read **dynamically from the OCDS schema and the 9+ active extensions per release** — not a fixed, easily-ported algorithm. Validation (`libcoveocds`) and collection precedents (Kingfisher) are also Python. Re-implementing all of this in TypeScript was, on inspection, plausibly the single largest work item and the `ocdsRecordPackages` shortcut that might have avoided it is FTS-only and single-OCID/unpaginated, so it doesn't help for 4 of the 5 sources.

## Considered options

- **Python ingestion + TS API (chosen)** — each runtime where it's strongest; merge is solved, not ported.
- **All TypeScript with a conformance-tested merge port** — single toolchain, but a real risk-bearing port across all extensions and a CF OCDS-1.0 up-conversion.
- **All TypeScript, sidestep merge via source compiled records** — rejected: not viable at scale (FTS record endpoint single-OCID/unpaginated; other sources vary).

## Consequences

Two toolchains (matching govreposcrape's operational reality and its existing Dockerfiles/patterns). The repo carries a Python ingestion package and a TS API package. An OCDS-merge conformance suite (against OCP's published merge fixtures) is still required to guard our use of the library and the per-source up-conversions feeding it.
