# UK Tenders MCP

A structured, queryable index of UK public-procurement data aggregated from the national tender portals (**Find a Tender**, **Contracts Finder**, **Public Contracts Scotland**, **Sell2Wales**, **eTendersNI**), normalised into one OCDS-based model and exposed over MCP for AI assistants. Optimised for historical/analytical querying of the full corpus, refreshed nightly. (Repo formerly `find-tender-mcp`; FTS is the reference source, not the only one.)

## Language

### Core entities

**Contracting process**:
The full lifecycle of a single procurement, identified by one **OCID**, spanning planning → tender → award (and amendments). This is the canonical meaning of **"tender"** in this project and the spine of the index.
_Avoid_: "tender" used loosely for the whole process — say **contracting process** when precision matters; reserve "tender" for casual reference and the FTS-facing label.

**OCID**:
Open Contracting ID — the source identifier for a **contracting process**. FTS uses the prefix `ocds-h6vhtk-`. Usually 1:1 with a real procurement, but occasionally a single real procurement is split across several OCIDs (known FTS data-quality issue).

**Process group**:
A derived identity that links the OCIDs believed to belong to one real procurement, resolved by best-effort matching on (buyer, title, value, CPV, dates) — including *across* **sources**, since OCIDs are publisher-scoped. Exposed alongside the raw **OCID** so callers can choose source-fidelity or stitched-accuracy.

**Regime**:
Which legal/notice framework a **release** belongs to — `pca2023` (Procurement Act 2023, from 2025-02-24, notice types UK1–UK17) or `legacy` (PCR2015 etc., TED-style F/T forms). Not flagged in the source feed; **derived** from publication date and notice/form code. (Scotland runs its own regulations; regime is primarily an England/Wales/NI distinction.)

**Source** (a.k.a. **Portal**):
One of the originating publishers aggregated into the index — **Find a Tender** (FTS), **Contracts Finder**, **Public Contracts Scotland**, **Sell2Wales**, **eTendersNI**. Each is normalised into the common model by its own adapter. An **OCID** is unique only *within* a source (publisher-scoped prefix), so cross-source identity relies on the **process group**.

**Release**:
One published event/snapshot within a **contracting process** (e.g. tender published, award made, amendment). Append-only; the source of truth for change history.

**Record**:
The compiled current-state view of a **contracting process**, derived from all its **releases**. (OCDS "record package".)

**Notice**:
The FTS-published document a buyer issues; maps to a **release**. Identified by a notice ID of the form `nnnnnn-yyyy`.

**Stage**:
One of `planning`, `tender`, `award` — the FTS API's `stages` filter values. Note: `tender` as a stage is narrower than **tender** the casual term (= **contracting process**).
_Flagged ambiguity, resolved below._

**Award**:
A decision within a **contracting process** recording who won and the contract value. A process may have multiple awards.

**Party** (a.k.a. **Organisation**):
A buyer or supplier participating in a **contracting process**. Referenced by release data; many lack stable identifiers (known data-quality issue).

**Update**:
A newly published or revised **release** on an existing **contracting process** since the last ingest run. Detected **per source** via that source's incremental mechanism (FTS `updatedFrom`, Contracts Finder `publishedFrom`, PCS/Sell2Wales month windows, eTendersNI scrape-diff). The basis of nightly refresh and "what changed" queries.

**Awarded value**:
The monetary value recorded on an **award** (and/or contract) — the value of the contract as awarded. **Not** actual spend/payments: the sources publish no transaction data, so "spend" language is avoided in favour of **awarded value**.
_Avoid_: Spend, expenditure (these imply cashflow the data does not contain).

**Freshness** (`data_current_as_of`):
The recency signal returned on responses: **per source**, the last successful sync time and health, plus an overall worst-case. Responses flag when an in-scope **source** is degraded or excluded.

## Relationships

- A **contracting process** is identified by one **OCID** and is composed of one or more **releases**; a **process group** may link several **OCIDs** that represent the same real procurement.
- A **release** belongs to exactly one **contracting process** and corresponds to one **notice**.
- A **record** is the compiled state of one **contracting process**, derived from its **releases**.
- A **contracting process** has zero or more **awards** and references one or more **parties**.
- An **update** is a new or revised **release** appended to an existing **contracting process**.

## Example dialogue

> **Dev:** "When a buyer amends a deadline, do we get a new **tender**?"
> **Domain expert:** "No — same **contracting process**, same **OCID**. You get a new **release** (an **update**) on the existing process. We append it to the event log and recompile the **record**."
> **Dev:** "And if I filter `stages=tender`, that's all live opportunities?"
> **Domain expert:** "That's the `tender` **stage** of processes — not the same as 'all tenders'. A process can also have `planning` and `award` stages."

## Flagged ambiguities

- **"tender"** was used to mean both the whole procurement lifecycle and the narrow `tender` **stage**. Resolved: the canonical project meaning is **contracting process** (keyed by **OCID**); the `tender` **stage** is always qualified as a "stage".
- **"update"** vs **"record"**: an **update** is the *event* (a new **release**); the **record** is the *recompiled current state* after applying it.
- **OCID as process identity**: source OCIDs are not perfectly 1:1 with real procurements. Resolved: the **OCID** is the source key; a derived **process group** stitches likely-duplicate OCIDs for accurate aggregation. Aggregation tools expose both.
