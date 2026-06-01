# UK Tenders MCP — Product Requirements Document

**Author:** cns
**Date:** 2026-05-30
**Version:** 0.2 (draft; revised after adversarial review)
**Repo:** `uk-tenders-mcp` (formerly `find-tender-mcp`)
**Related:** [CONTEXT.md](../CONTEXT.md) (glossary) · ADRs [0001](adr/0001-bigquery-index-with-gcs-raw-and-star-schema.md) [0002](adr/0002-canonical-model-source-adapters-process-group.md) [0003](adr/0003-public-mcp-readonly-sql-secured-by-iam.md) [0004](adr/0004-launch-with-all-five-sources.md) [0005](adr/0005-python-ingestion-typescript-api.md)

---

## 1. Executive summary

UK public-procurement data is open and standardised (OCDS, Open Government Licence v3.0) but **fragmented across five national portals**, each with a different API, schema version, and identifier scheme — and **the same procurement is published on several of them**. Anyone wanting to ask questions across the whole picture ("how much was *awarded* to cloud-hosting suppliers by NHS bodies in 2023?", "which buyers extended deadlines most often?", "what changed on this tender since last night?") has no single, deduplicated, machine-friendly surface.

**UK Tenders MCP** is a remote [Model Context Protocol](https://modelcontextprotocol.io) server that mirrors the UK procurement corpus (earliest source from 2016; FTS from 2021) into one analytics-ready index and exposes it to AI assistants. It is **analytics-first**: optimised for historical and cross-cutting questions over the whole corpus, not live opportunity discovery. It mirrors the [`govreposcrape`](../../govreposcrape) architecture — a TypeScript Cloud Run MCP API over a managed Google Cloud index, fed by a **Python** scheduled ingestion job ([ADR-0005](adr/0005-python-ingestion-typescript-api.md)) — but where govreposcrape does *semantic search over unstructured text*, this does *structured analytics over OCDS records*.

Every result links to the **official notice URL**; every response signals **how fresh** the data is, per source. The index is a faithful, attributed, **redacted** mirror — never the authority of record.

---

## 2. Problem & opportunity

- **Fragmentation.** Find a Tender (FTS), Contracts Finder, Public Contracts Scotland (PCS), Sell2Wales and eTendersNI each publish independently; coverage, thresholds, schema versions and identifiers differ (§7.1).
- **Duplication by design.** Above-threshold notices are cross-posted by law; PCS and Sell2Wales auto-forward to FTS. Naive aggregation double-counts — deduplication is mandatory ([ADR-0002](adr/0002-canonical-model-source-adapters-process-group.md)).
- **No server-side querying.** The FTS OCDS API supports *no* filtering beyond `stages` + date/cursor — no CPV, value, region, buyer or status filters at source. **All query power must live in our index.** This is the core justification for the product.
- **No machine-friendly assistant surface.** Portals are built for human browsing or bulk download, not conversational/analytical interrogation by an AI assistant.

---

## 3. Goals & non-goals

### Goals
1. A **complete, deduplicated, analytics-ready** index of UK public-procurement OCDS data across all five portals.
2. **Analytics-first** querying across awarded value, CPV, buyer, supplier, region, time, status, regime.
3. First-class **"what changed"**: a materialised field-level change feed and per-process change timelines.
4. **Faithful provenance & lawful handling**: official notice URL on every record; per-source freshness on every response; OGL v3.0 attribution; **personal data redacted** from the public surface (§10.1).
5. **Frictionless public access** over MCP (no auth).
6. **Low, capped running cost** (scale-to-zero serving; per-query and per-day spend caps sized from §15).

*(Engineering parity with govreposcrape is a delivery approach, not a product goal — see §11/§16.)*

### Non-goals (v1)
- Not a live-bidding/alerting product (no push/subscriptions — updates are pull-based, as-of last night's ingest; §9).
- Not an authority of record — always defer to the official notice.
- Not a spend/cashflow dataset — sources publish **no transactions** and **no bidder lists**; "value" means **awarded contract value**, never payments (bar new UK17 payment-compliance notices). Tool names and outputs say "awarded value", not "spend".
- Not re-aggregating third-party aggregators (OpenTender, OpenOpps) as primary sources.

---

## 4. Primary users & use cases

**Primary (analytics-first, well-served):**

| Persona | Representative question | Tool path |
|---|---|---|
| **Procurement analyst / researcher** | "Total *awarded value* by CPV division for NHS bodies, 2022–2024, by quarter." | `aggregate_tenders` / `query_sql` |
| **Journalist / transparency** | "Which buyers issued the most direct awards (UK5/transparency) since the Act came in?" | `aggregate_tenders` + `regime` filter |
| **Market intelligence** | "Top 20 suppliers by total awarded contract value in 'works', and how concentrated is it?" | `top_suppliers` / `query_sql` |

**Secondary (answerable, but NOT the job-to-be-done — point-in-time only, as-of last ingest):**

| Persona | Caveat |
|---|---|
| **Supplier / bid team** ("open IT tenders over £1m closing in 30 days") | Best-effort point-in-time query; **not a deadline-alerting tool** — see non-goals. Up to ~24h stale. |
| **Anyone tracking a procurement** ("what changed since I looked?") | Returns "what changed **as of last night's ingest**", not real-time. |

The consumer is an **AI assistant** calling MCP tools; result shaping (§8.3) keeps responses token-efficient.

---

## 5. Scope: sources & launch shape

- **All five portals at launch** — no phased public rollout ([ADR-0004](adr/0004-launch-with-all-five-sources.md)).
- Internal build order (prove the canonical model + matcher on easy sources first): **FTS → Contracts Finder → PCS → Sell2Wales → eTendersNI**.
- **Adapter isolation**: each source ingests and fails independently; a down/stale source is flagged via `get_status`, never fatal to the rest.
- **eTendersNI launch gate** (§12) with pre-agreed outcomes — native EPPS feed, OpenOpps contingency, or escalate scope — so one fragile scraper cannot silently sink the launch.
- **Excluded as primary sources:** OpenTender, OpenOpps (re-aggregators that re-mint OCIDs). OpenOpps retained only as a *contingency* NI feed.

---

## 6. Domain model

See [CONTEXT.md](../CONTEXT.md) for the authoritative glossary. Spine concepts: **contracting process** (one real procurement; canonically "tender"), **OCID** (source id, *publisher-scoped*), **process group** (derived cross-source identity), **release** (one published event), **record** (compiled current state), **regime** (`pca2023` vs `legacy`, derived), **source/portal**, **update** (per-source), **awarded value** (not spend).

**Regime derivation** (user-facing filter, so pinned): map notice/form code → regime via the §13 lookup; tie-break notices in the 24 Feb 2025 cutover window by code first, date second; legacy notices arriving post-cutover stay `legacy`; Scotland = `scotland` (its own regulations); regime is assigned **per release**, and a process spanning both is reported as `mixed`.

---

## 7. Data sources & ingestion

### 7.1 Source matrix

| Source | OCDS | API base / mechanism | Prefix | Incremental | Coverage | Net-new value | Difficulty |
|---|---|---|---|---|---|---|---|
| **Find a Tender (FTS)** | 1.1.5 | `…/api/1.0/ocdsReleasePackages` (+ `ocdsRecordPackages` single-OCID) | `ocds-h6vhtk` | `updatedFrom`/`updatedTo` + `cursor` | 2021→now; below-threshold from 24 Feb 2025 | The hub | **Easy** |
| **Contracts Finder** | v1.0 (+partial 1.1) ⚠️ | `…/Published/Notices/OCDS/Search`; bulk CSV harvester | `ocds-b5fd17` | `publishedFrom`/`publishedTo` + `cursor` | 2016→now | Pre-2025 England below-threshold | **Easy–Medium** |
| **Public Contracts Scotland** | EU-profile | `api.publiccontractsscotland.gov.uk/v1` ⚠️ | `ocds-r6ebe6` | month windows (Kingfisher precedent) ⚠️ | 2019→now | **Scottish below-threshold (sole source)** | **Medium** |
| **Sell2Wales** | EU-profile | `api.sell2wales.gov.wales/v1` ⚠️ (vs `…klickstream.com`) | `ocds-kuma6s` | likely PCS-like ⚠️ | 2016→now | Welsh sub-threshold/legacy | **Medium** (reuses PCS adapter) |
| **eTendersNI** | **None** | HTML scrape of `/epps/` ⚠️ | mint our own | scrape-diff | — | NI below-threshold local (and possibly NI above-threshold if FTS auto-forward is unreliable ⚠️ §12) | **Hard** |

⚠️ = needs live verification before adapter build (§12). Sources: [FTS API](https://www.find-tender.service.gov.uk/apidocumentation/1.0/GET-ocdsReleasePackages), [CF API](https://www.contractsfinder.service.gov.uk/apidocumentation), OCP registry [FTS](https://data.open-contracting.org/en/publication/41)/[CF](https://data.open-contracting.org/en/publication/128)/[PCS](https://data.open-contracting.org/en/publication/39)/[Sell2Wales](https://data.open-contracting.org/en/publication/119).

### 7.2 FTS API mechanics (reference adapter)
- **Harvest via release packages only** — the record endpoint is single-OCID and unpaginated.
- **Pagination (FTS, verified):** follow the response's `links.next` URL *verbatim*; stop when the `links` key is **absent**; the cursor is opaque (never construct it); serial within a window. **This rule is verified for FTS only** — CF cursor termination is unverified and PCS/Sell2Wales pagination is entirely unconfirmed (⚠️ §12); each adapter must confirm its own termination signal.
- **Rate limits:** none published; honour `Retry-After` on 429/503 with exponential backoff.
- **Date format:** `YYYY-MM-DDTHH:MM:SS`, no offset. ⚠️ Timezone (Europe/London vs UTC) is doc-inferred — **verify empirically before trusting across DST**; mitigate with overlapping windows.
- **9 OCDS extensions** in use; **extension URLs must be pinned and verified against live payloads** (the EU-profile URL differs: docs `/master/` vs live `/latest/`), because merge rules are resolved from them ([ADR-0005](adr/0005-python-ingestion-typescript-api.md)).

### 7.3 Per-source adapters (Python)
Each adapter: harvests via its native incremental mechanism → **up-converts to canonical OCDS 1.1.x** (CF's 1.0 is up-converted before merge) → archives raw to GCS → loads to the release event-log. Publisher versions/extensions are absorbed here.

**eTendersNI synthetic OCID** (launch-critical, [ADR-0004](adr/0004-launch-with-all-five-sources.md)): mint a deterministic OCID = hash over a **stable upstream natural key** (the EPPS notice reference), **never** over rendered HTML or scrape order, so re-scrapes are idempotent and don't fork into phantom duplicate processes. The OpenOpps contingency feed must preserve the *same* minted-id mapping. Covered by a golden idempotency test (§10.4).

### 7.4 Backfill & nightly refresh — one code path
**Backfill and nightly are a single, windowed-replay code path**, not two programs. The same ingestion entrypoint is parameterised by window: backfill walks fixed historical windows (per-source floor: FTS 2021, CF 2016, PCS 2019, Sell2Wales 2016); nightly replays from the last watermark **minus a safety lookback**. There is no separate backfill-only code.

- **Idempotent load:** `MERGE` on a deterministic **content hash** (SHA-256 of canonicalised release JSON — canonicalisation is RFC 8785 JCS, or an explicit versioned recipe: sort keys, normalise numbers, strip enumerated volatile/ingest-meta fields). Do *not* assume `(ocid, release_id)` is unique. The canonicalisation recipe is versioned so an adapter change doesn't silently double-load.
- **Withdrawals:** sources unpublish notices. On re-harvest of a window, detect absence and write a **tombstone/status** so compiled state and aggregates exclude withdrawn procurements (an append-only log alone would over-count them forever).
- **Recompute correctly:** after a run, recompile touched OCIDs and rebuild derived layers. The cross-source **affected-set is the full blocking-key block** any touched OCID falls into (a new release can pull a previously-unmatched OCID into an existing group) — or accept a periodic full re-match at a stated cadence. Derived layers are built into staging and **atomically swapped** (or via an `as_of` snapshot id); readers never see a half-recompute; a failed run keeps the last good snapshot.

---

## 8. The index & MCP interface

### 8.1 Data architecture ([ADR-0001](adr/0001-bigquery-index-with-gcs-raw-and-star-schema.md))

```
Source portals ─(Python adapters)─► GCS raw archive (immutable, PII-bearing, access-controlled; replay source)
                                          │
                                          ▼
   ┌─────────────── uk_tenders_raw  (BigQuery, WRITE dataset, PII) ───────────────┐
   │  release_event_log (append-only, full JSON + hot scalars)                    │
   │      │ compile (ocds-merge, per OCID, deterministic)                         │
   │      ▼                                                                       │
   │  compiled_process · star schema · process_group · process_change · ingest_run│
   └──────────────────────────────── redact + build-and-swap ────────────────────┘
                                          │
                                          ▼
   ┌─────────────── uk_tenders_public (BigQuery, READ dataset, redacted) ─────────┐
   │  query-serving tables/views (no personal data) ◄── public read-only SA       │
   └──────────────────────────────────────────────────────────────────────────────┘
```

- **Two datasets** ([ADR-0001](adr/0001-bigquery-index-with-gcs-raw-and-star-schema.md)): `uk_tenders_raw` (write, PII) vs `uk_tenders_public` (read, redacted). The public read-only service account ([ADR-0003](adr/0003-public-mcp-readonly-sql-secured-by-iam.md)) sees only the public dataset/views.
- **Raw event log**: lossless, partitioned by load date, clustered by (source, ocid, release_id).
- **Compiled layer**: ocds-merge per OCID — sort releases by date ascending; the canonical tie-break is input-order (non-deterministic), so we impose a **deterministic secondary sort on release id** before folding (later values win; nulls delete; id-keyed arrays merge per element). Fully rebuildable.
- **Star schema**: hot scalars flattened to columns + nested `ARRAY<STRUCT>` for repeatable groups (`UNNEST` on read); full object retained too.
- **process_group**: synthetic id linking matched OCIDs + confidence (§8.5).
- **process_change**: field-level diffs at ingest (§9).
- **ingest_run** ledger: one row per adapter run (§8.6).
- A **schema appendix / `sql/` DDL directory** is a required deliverable: dataset names, every table's columns + types, partition column + clustering keys, the public views, and the exact IAM bindings. `get_schema` reads from it.

### 8.2 MCP tools ([ADR-0003](adr/0003-public-mcp-readonly-sql-secured-by-iam.md))

| Tool | Purpose | Key params |
|---|---|---|
| `search_tenders` | Filtered discovery | keyword, CPV, buyer, supplier, value range, status, stage, dates, region, regime, source; `resultMode`; cursor pagination |
| `get_tender` | One process: compiled record + **change timeline** + official URL | OCID or notice id; `resultMode` |
| `list_updates` | What changed since a timestamp (as-of last ingest) | `since`, filters; `resultMode` |
| `aggregate_tenders` | **Flexible analytics engine** | metric (count/sum/avg/min/max/**median**) × dimension (buyer/supplier/CPV[division\|full]/stage/status/region/regime/time-bucket) × filters; `stitch` (raw OCID vs process group) |
| `top_suppliers`, `awarded_value_by_buyer`, `awards_over_time`, `cpv_breakdown` | Named shortcuts over the engine | common filters |
| `query_sql` | **Read-only SQL** over the public dataset | `sql` (IAM-enforced read-only; dry-run cost-estimated against `maximum_bytes_billed`) |
| `get_schema` | Table/column introspection + the byte cap value | — |
| `get_status` | Per-source freshness/coverage/health (from `ingest_run`) | — |

**Value metrics** (`sum`/`median` of value): always grouped by currency — **never summed across currencies**; `median` uses `APPROX_QUANTILES` and is **labelled approximate** (exact percentile over high-cardinality groups can breach the byte cap). Given the value-outlier data issue (§12), value summaries default to median + IQR alongside sum.

**Error contract (all tools):** a uniform envelope `{ code, message, hint }`. Defined cases include: `query_sql` cost-exceeds-cap (structured "would scan ~N bytes; cap is M" *before* executing), timeout, syntax error, attempted DML (refused); `get_tender` unknown/ambiguous id; `aggregate_tenders` invalid metric×dimension or cross-currency sum (rejected with a "group by currency" hint); pagination (opaque cursor, max page size, whether a total count is available — stated explicitly); upstream 429 surfaced as retryable.

### 8.3 Result shaping (token economy)
`resultMode` on every list/get tool: `minimal` (default — id, title, buyer, **awarded/estimated value**, status, key dates, **official URL**), `standard` (+ description, CPV, lead award & parties), `full` (complete compiled record — **redacted public dataset only**). The official notice URL (e.g. `https://www.find-tender.service.gov.uk/Notice/{noticeId}`, per-source equivalents) appears in **every** mode.

### 8.4 Provenance & freshness
Official URL + per-record `last_updated` on every record. Every response carries a **per-source** freshness envelope (`source → last_successful_sync + status`) plus an overall worst-case `data_current_as_of`; responses **flag when any in-scope source is degraded or excluded**. OGL v3.0 attribution + "verify critical details on the official notice" in tool descriptions.

### 8.5 Cross-source identity ([ADR-0002](adr/0002-canonical-model-source-adapters-process-group.md))
Within-source: compile by OCID (deterministic). Cross-source: **blocking** (buyer, CPV, value band, month) → **fuzzy scoring** (normalised buyer, title, value+currency, dates, CPV, notice refs) → `process_group` + confidence. Aggregations default to **raw OCID** (faithful); `stitch=true` collapses to process groups. **Both can mislead** — `stitch=off` inflates totals (double-counts cross-posts), `stitch=on` risks mis-merges — so aggregate responses surface the active mode + a confidence note, and the default is documented loudly. Evaluation plan + launch thresholds: see ADR-0002 and §14.

### 8.6 Health ledger
Every adapter run writes an `ingest_run` row: `run_id, source, started_at, finished_at, status, watermark_from/to, releases_seen, releases_loaded, error_summary`. `get_status` and alerting (§10.5) compute health from it: **red** if no successful run in >36h or last run failed; **amber** if stale-but-degraded; **green** otherwise — and it distinguishes "ran, no new notices" from "did not run."

---

## 9. Updates & change tracking

A **materialised** field-level change feed. At ingest, diff successive states of each process → `process_change` (process, field path, old → new, release id, timestamp, **change class**). `get_tender` returns a change timeline; `list_updates(since)` returns processes changed since a timestamp; `aggregate_tenders`/`query_sql` make cross-cutting change-analytics queryable.

- **Change-class taxonomy is provisional**: which classes we can populate (deadline-extended, value-changed, cancelled, new-award, …) depends on **which release tags FTS actually emits** (contract/implementation/amendment/cancellation — open §12 Q). Treat as provisional until verified.
- **Noise filter:** a benign re-publish that only churns a volatile field must **not** surface as a real change; the canonicalisation strip-list (§7.4) and a change-class filter exclude re-export/value-misreport noise (§12 data-quality).

---

## 10. Non-functional requirements

### 10.1 Data protection (UK GDPR / DPA 2018) — launch-blocking
OCDS notices contain personal data (`parties[].contactPoint.{name,email,telephone,faxNumber,url}`, named sole traders/partnerships, free-text fields). OGL v3.0 permits reuse but does **not** extinguish our obligations as a re-publisher.

- **DPIA** completed before launch; enumerate every personal-data OCDS path.
- **Redaction at the boundary:** personal fields are stripped/hashed when building `uk_tenders_public`; **raw-with-PII stays only in the access-controlled `uk_tenders_raw` + GCS tier.** `full` resultMode and `query_sql` operate **only** over the redacted public dataset.
- **Lawful basis** stated; a **published takedown/erasure process** and contact; retention policy for the raw tier (§10.6).

### 10.2 Cost
Scale-to-zero Cloud Run; BigQuery `maximum_bytes_billed` per query + a **daily project spend cap with alerting** (launch value set from §15 sizing, not inherited). Target: monthly cost ≤ a budget set from §15 at projected adoption.

### 10.3 Security
Public read; least-privilege read-only SA for the API (public dataset only); ingestion writes via a separate identity; PII isolation (§10.1); per-IP/connection rate limiting; statement timeouts; byte caps (the real backstop).

### 10.4 Testing
- Per-adapter **golden-file** tests (raw fixture → expected canonical OCDS); **recorded-cassette** HTTP — **no live-source calls in CI**.
- **OCDS-merge conformance** suite against OCP's published merge fixtures (guards [ADR-0005](adr/0005-python-ingestion-typescript-api.md) + CF 1.0 up-conversion).
- **Matcher evaluation** harness with a committed gold set + metric script (§14).
- **MCP tool contract** tests incl. the error envelope (§8.2).
- A **security test** asserting the read SA cannot mutate or exceed the byte cap (the ADR-0003 guarantee).
- eTendersNI **synthetic-OCID idempotency** golden test (§7.3).

### 10.5 Observability & alerting
A monitoring matrix `{metric, threshold, severity, destination}`. Minimum: nightly-ingest failed/stalled per source; watermark-not-advanced; per-source release-count anomaly (drop to ~0 despite "success"); BigQuery daily bytes/spend vs cap; API 5xx/latency/uptime; rate-limit trips. Alert channel + response owner named.

### 10.6 Data lifecycle & versioning
GCS path/partition convention + per-run load manifest so a full rebuild is reproducible and its **runtime measured as a recovery SLO**. Raw-tier retention policy (it is the PII-holding tier — §10.1). Derived-table schema evolution (likely, given PA2023's ~143 new fields) via versioned datasets / build-and-swap, no downtime. **Reference-data versioning** (§13): store the ref-data version used in each derivation; define recompute-vs-stamp policy when a codelist (CPV/ITL) changes.

### 10.7 Deployment & CI/CD
**Terraform** provisions the two datasets + distinct IAM, the GCS bucket(s), Cloud Run service (TS API) and Cloud Run Jobs (Python adapters + orchestrator), and Cloud Scheduler — so the read/write separation is reproducible. At least a **staging dataset** to validate derivations/matcher before promotion. Schema-migration + reference-data load as deploy steps. A deploy-time check asserts the two service accounts' IAM. Politeness to sources: adaptive backoff, `Retry-After`, conservative poll rates.

### 10.8 Licensing & accessibility
OGL v3.0 attribution surfaced to users; MIT for our code. Any web/status page meets WCAG 2.1 AA.

---

## 11. Tech stack & deployment

- **Languages ([ADR-0005](adr/0005-python-ingestion-typescript-api.md)):** **Python** ingestion (adapters, `ocds-merge`/`ocdskit` compile, `libcoveocds` validation, matching) + **TypeScript** MCP API. Matches govreposcrape's real split.
- **Transport:** Streamable-HTTP MCP on Cloud Run (SSE fallback).
- **GCP:** Cloud Run (API), Cloud Run Jobs + Cloud Scheduler (nightly ingest), Cloud Storage (raw archive), BigQuery (two datasets).
- **Note on the record-endpoint shortcut:** `ocdsRecordPackages` (FTS-only, single-OCID, unpaginated — ~184k+ requests for FTS alone) does **not** avoid building compilation for the other four sources; compile in-pipeline via ocds-merge.
- **Distribution (govreposcrape parity):** Claude Code plugin + marketplace, skills, `.mcp.json`, `claude mcp add --transport http …`, Claude Desktop config, OpenAPI/health endpoints.
- **Reference data shipped in-repo** (§13).

---

## 12. Risks & open questions

**Critical-path — eTendersNI launch gate (date-boxed, three pre-agreed outcomes):**
1. native EPPS JSON/RSS feed found → build adapter;
2. else OpenOpps NI viable → use as contingency (accept re-minted-OCID caveat; preserve minted-id mapping, §7.3);
3. else → escalate launch scope: option to ship four sources and flag NI as "coming" (reconciles ADR-0004's "all five" with its own graceful-degradation safety net).

**Data quality (high):** missing org identifiers → buyer/supplier aggregation uses name-normalisation, not `parties.identifier`; malformed dates / value outliers / repeated values → defensive parsing, flag implausible deltas, default value summaries to median+IQR; sparse linkage (~9% awards link tender↔contract).

**Dual regime:** no flag; infer from date + notice code. Below-threshold history **overlaps** rather than hands over — pre-2025 only on Contracts Finder; from 24 Feb 2025 **also** on FTS (except Scotland) — so stitch across prefixes at the cutover *and* dedupe the post-2025 overlap. (CF below-threshold VAT-inclusivity is itself ⚠️.)

**Cross-source matcher:** no ground truth; both stitch modes can mislead (§8.5); evaluation + thresholds in §14/ADR-0002.

**To verify before adapter build (⚠️):** (1) FTS `updatedFrom` timezone/DST + semantics (creation vs modified vs ingestion time); (2) `stages` multi-value support and which release tags FTS emits (gates §9 taxonomy and whether contract/implementation are retrievable); (3) whether FTS populates `amendments` (reliable change pointer vs diff-only); (4) PCS/Sell2Wales exact endpoints/pagination/auth and Sell2Wales host (`api.sell2wales.gov.wales` vs `…klickstream.com`); (5) CF OCDS version status + rate limit + whether the CF feed is frozen or live post-2025; (6) geography namespace (NUTS `UKxxx` vs ITL `TLxxx`) in recent payloads; (7) whether CDP introduces a stable org-identifier scheme (a major dedup unlock); (8) NI above-threshold auto-forward reliability to FTS.

Each ⚠️ is tagged to the acceptance criterion it gates (§17).

---

## 13. Reference data to ship

Nine small, static, **version-pinned** lookup tables (no server-side filtering exists at source, so all categorisation lives here): **CPV** (~9,454 codes, 8-digit, prefix roll-up 2=division/3=group/4=class/5=category) + **45 divisions**; **geography** (NUTS↔ITL crosswalk + names, L1 12-region default); **procedure types** (eForms 8-value + coarse OCDS method); **statuses** (separate `tender`/`award`/`contract` codelists — do not collapse); **party roles**; **notice types** (UK1–UK17 + legacy F/T → name → **regime** → stage; drives §6 regime derivation); **ISO-4217 currencies**; **mainProcurementCategory** (goods/works/services; OCDS "goods" = CPV "supplies"). Values stored as {amount, currency} — never summed across currencies; optional date-keyed FX table for GBP normalisation. Each table is versioned; the version used in a derivation is stamped (§10.6).

---

## 14. Success metrics (v1) — measurable

Each as {metric · method · target}:
- **Coverage (self-consistency):** ≥99% of releases returned by a full re-walk of the last 30 days are present within 24h · re-walk vs index diff · *absolute coverage vs source totals = Not Measurable* (no source publishes a census).
- **Dedup quality:** precision ≥0.9 on merges (hand-labelled gold set); recall reported via the **auto-forward proxy** (PCS/S2W/NI→FTS known-true pairs) · matcher eval harness (§10.4) · gate before `stitch=true` is any default.
- **Freshness:** per-source `data_current_as_of` < 24h under normal operation · `ingest_run` ledger.
- **Adoption:** weekly MCP tool calls + distinct clients · Cloud Run request logs tagged by MCP tool name · 90-day target TBD at launch.
- **Answer quality:** monthly spot-check of 20 random aggregate answers re-derived from source ≥95% match (within rounding/currency) · manual protocol, named owner.
- **Cost:** monthly cost ≤ budget (§15) · BigQuery + Cloud Run billing export vs daily cap.
- **Reliability:** per-source ingest success rate; graceful degradation verified (a down source never breaks queries on the others) · `ingest_run` + an integration test.

---

## 15. Backfill sizing & cost (to produce before sprint 1)

A go/no-go input, not optional. Per source, estimate: expected release counts; harvest wall-clock at conservative poll rates (and whether it fits a Cloud Run Job max-runtime or needs **checkpoint/resume**); GCS bytes; and BigQuery byte/slot cost of the **initial full derivation + full cross-source match** over ~1–2M releases — with **block-size capping** for skewed buyers (NHS/MoD) so matching doesn't blow up quadratically. The **daily spend cap (§10.2) and monthly budget (§14) are set from these numbers.** FTS replay floor is ~5–6k serial cursor requests (research, medium confidence); eTendersNI adds scrape time.

---

## 16. Acceptance criteria (v1 "done when…")

A spine for engineers; each ties to Goals/NFRs and tags the §12 question it gates:
- **AC-1** A query for a known FTS notice returns its official URL in all three `resultMode`s.
- **AC-2** With PCS marked stale in `ingest_run`, search/aggregate over the other sources still returns within SLA and the response flags PCS degraded.
- **AC-3** `query_sql` attempting DML or a >cap scan returns a structured error and performs no write (security test).
- **AC-4** No `contactPoint` personal field appears in any `uk_tenders_public` table/view or any tool response (PII test).
- **AC-5** Re-scraping eTendersNI for an unchanged notice produces no new OCID and no `process_change` row (idempotency test).
- **AC-6** `aggregate_tenders` sum across mixed currencies is rejected with a remediation hint.
- **AC-7** OCDS-merge conformance suite passes against OCP fixtures (incl. one CF 1.0 → 1.1 up-conversion case).
- **AC-8** A killed nightly run surfaces as `red` in `get_status` within 36h and fires an alert.

---

## 17. Appendix — house lineage

This PRD mirrors [`govreposcrape`](../../govreposcrape)'s shape: a TypeScript Cloud Run MCP read API over a managed GCP index, fed by a **Python** scheduled ingestion job, distributed as a Claude Code plugin, public and unauthenticated, cost-capped. The defining difference is the **data model**: govreposcrape indexes unstructured text for *semantic search*; UK Tenders MCP indexes structured, multi-source, deduplicated OCDS for *analytics* — which is why this document centres on the canonical model, cross-source identity, PII redaction, and the analytics tool surface rather than relevance tuning.
