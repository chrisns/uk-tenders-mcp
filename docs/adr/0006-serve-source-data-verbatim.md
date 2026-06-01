# Serve source data verbatim — no PII redaction

**Status:** accepted (revises the redaction boundary described in [ADR-0001](0001-bigquery-index-with-gcs-raw-and-star-schema.md) and [ADR-0003](0003-public-mcp-readonly-sql-secured-by-iam.md))

The index re-publishes UK procurement OCDS **verbatim**: each release is reproduced exactly as published by the source authority, with no field stripped, hashed, or altered. An earlier design redacted `parties[].contactPoint` personal fields (name/email/telephone/fax) when building the public dataset; that redaction is **removed**.

## Why

- **The data is already public.** Every record is published as open data by its source authority (Find a Tender, Contracts Finder, Public Contracts Scotland, Sell2Wales, eTendersNI) under the Open Government Licence v3.0. We re-publish an already-public record — we are not disclosing anything new.
- **We are not the authority of record.** The index is a faithful mirror; the source authority is the data controller. Altering content makes the mirror *less* faithful and harder to reconcile against the official notice — the opposite of the product's provenance promise. Every record links to the official URL for verification.
- **Redaction was lossy and inconsistent.** It touched only the OCDS `contactPoint` path on the API-fed sources — never the free-text `tender.title`/`tender.description` fields, and never the scrape-sourced rows. It gave the *appearance* of PII protection without the substance. Verbatim is honest about what the dataset is.
- **The least-privilege boundary is unaffected.** Dropping redaction does **not** change the IAM model: the public read-only service account still sees only `uk_tenders_public`; the full raw event log + GCS archive stay in the access-controlled write tier ([ADR-0001](0001-bigquery-index-with-gcs-raw-and-star-schema.md), [ADR-0003](0003-public-mcp-readonly-sql-secured-by-iam.md)). That split now serves blast-radius containment, not content filtering.

## Considered options

- **Serve verbatim (chosen)** — faithful, attributable, honest; relies on the source authorities' own basis for publishing the data as open, plus a takedown route as the residual control.
- **Keep `contactPoint` redaction** — rejected: lossy/inconsistent (above), reduces fidelity, and implies a PII-safety guarantee the free-text and scrape paths never met.
- **Full PII detection + redaction across all free text** — rejected: high-effort, error-prone NLP over the whole corpus to suppress data that is already public at source; disproportionate to the risk.

## Consequences

- The public dataset may contain personal data exactly as the source authorities publish it (contact names/emails/phones; named sole traders; anything buyers typed into free-text fields). Documented in [`docs/OCDS-PII-mapping.md`](../OCDS-PII-mapping.md) and PRD §10.1.
- A **published takedown/erasure contact** is the residual control; such requests are also directed to the source authority as data controller.
- `canonical.py::redact()` and its tests are removed; `compile.project_process` writes the compiled release unchanged.
- The deployed `uk_tenders_public` had to be **rebuilt from the raw event log** (no re-scrape needed) for already-loaded rows to reflect the verbatim policy. **Done 2026-06-01:** FTS/CF/PCS (~664k processes) were reprojected from raw via `scripts/reproject_redacted.py`, restoring `contactPoint`; the scrape sources (Sell2Wales, eTendersNI) were never redacted, so they were carried over unchanged. The procedure is documented in [DEPLOYMENT.md](../../DEPLOYMENT.md) ("Rebuild the public dataset from the raw event log").
- **Sell2Wales:** rows already in the raw event log reproject verbatim like any other source, but *new* Sell2Wales data stays gated on the upstream OCDS API repair (its self-healing probe — see [DEPLOYMENT.md](../../DEPLOYMENT.md)); the verbatim switch does not change that.
