# UK Tenders MCP

Query the UK public-procurement corpus — every tender, award and amendment — directly from your AI assistant, over [MCP](https://modelcontextprotocol.io). Built on the official [Open Contracting](https://standard.open-contracting.org/) (OCDS) data, analytics-first, with the official notice URL on every result. ~677k procurement processes across all five UK portals.

**Live MCP endpoint:** `https://tenders.run.cns.me/mcp`

> Data is an attributed mirror of source procurement portals under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). It is **not** the authority of record — always verify critical details on the official notice.

## Use it

### Claude Code

```bash
claude mcp add --transport http uk-tenders https://tenders.run.cns.me/mcp
```

### Headless (one-shot)

```bash
claude -p "Use the uk-tenders MCP server: which organisations bought from Amazon, by value?" \
  --mcp-config .mcp.json \
  --allowedTools "mcp__uk-tenders__query_sql,mcp__uk-tenders__search_tenders,mcp__uk-tenders__top_suppliers,mcp__uk-tenders__awarded_value_by_buyer"
```

(Allow-list the read-only tools rather than `--dangerously-skip-permissions`.)

### Any MCP client

Point it at `https://tenders.run.cns.me/mcp` (Streamable HTTP). No authentication.

## Tools

| Tool | What it does |
|------|--------------|
| `search_tenders` | Filter by keyword, CPV, buyer, supplier, value, status, stage, regime, region, source, dates. Official URL on every result. |
| `get_tender` | One process by OCID or notice id — compiled state + **change timeline**. |
| `list_updates` | What changed since a timestamp (deadline/value changes, cancellations, new awards). |
| `aggregate_tenders` | Flexible analytics: count / sum / avg / min / max / median of **awarded value**, grouped by buyer, supplier, CPV, stage, status, regime, region, or time. |
| `top_suppliers`, `awarded_value_by_buyer`, `awards_over_time`, `cpv_breakdown` | Named shortcuts over the aggregate engine. |
| `query_sql` | Read-only BigQuery SQL over the public dataset (byte-capped; PII-free). The power tool. |
| `get_schema` | Tables/columns for `query_sql`. |
| `get_status` | Per-source freshness, coverage and health. |

Every result carries the official notice URL; every response carries a per-source freshness signal.

## Example questions

### Reuse opportunities — call off, don't re-procure
The highest-value use for a civil servant: before launching a fresh procurement, see what
peers already bought — to call off an existing framework, benchmark prices, or shortlist
incumbents.

- **"Is there a framework I can call off for cloud/IT instead of running my own?"** →
  Crown Commercial Service dominates CPV‑72 (IT) framework awards by a wide margin — the
  obvious call‑off route (G‑Cloud, Technology Services). `cpv_breakdown` / `top_suppliers`.
- **"What have peers paid Microsoft / AWS, to benchmark before I negotiate?"** → 337 itemised
  award lines name a Microsoft supplier; 223 name Amazon (~£2.49bn of award ceilings, top
  buyer Home Office ~£784m). Use the *per-notice* values, not the headline sum (see Caveats).
- **"Who already supplies cyber-security / consultancy across government, and at what scale?"**
  → `top_suppliers` + a keyword/CPV filter surfaces incumbents and the frameworks they sit on.
- **"Show me recent awards similar to what I'm about to buy"** → `search_tenders` by
  keyword/CPV; each hit links to the notice (and thus its framework / call-off terms).

### Spend & market analysis
- **"Which organisations bought from Amazon, and how much?"** → 223 award lines, top buyer
  Home Office (~£784m of ceiling). **"Top suppliers to the NHS"**, **"how has cloud spend
  trended?"** → `top_suppliers`, `awards_over_time`.

### Devolved / regional
- **"Biggest IT contracts in Wales this year"** → e.g. Welsh Government, *Hwb Educational
  Platform for Wales* (~£76m). Filter `region='UKL'`, `cpv_division='72'`. (`'UKN'` = NI.)

### Pipeline & live opportunities
- **"Active tenders closing in the next 60 days"** → ~5,000+ live notices; sort by value,
  each with deadline + URL. Cross-source — e.g. a live cyber SOC tender from Cardiff & Vale
  Health Board (Sell2Wales) sits alongside MoD (FTS) and CCS (Contracts Finder).
- **"What changed this week — cancellations or new awards?"** → `list_updates`.

## Caveats (the model is told these)

- **Awarded value = contract/framework *ceiling*, not actual spend.** Several notices carry
  placeholder ceilings (e.g. £9,999,999,999) or whole-framework maxima that massively inflate
  naive `SUM`s — round £10bn-per-supplier totals are a tell. Every result links to the
  official notice; verify critical figures there. Prefer per-notice values over aggregates.
- **Names aren't normalised at source** ("Ministry of Justice" vs "Ministry of Justice.").
  Cross-source dedup groups *processes* (`process_group`, ~24k cross-source/duplicate clusters
  linked), not entity names.
- **Freshness varies by source** — `get_status` reports it.

## Architecture

```
Source portals ──Python adapters──► GCS raw archive (PII) ──► BigQuery uk_tenders_raw (write, PII)
                                                                  │ compile (ocds-merge) + redact
   AI assistant ──MCP──► Cloud Run (TS API, read-only SA) ──► BigQuery uk_tenders_public (read, redacted)
                                                                  ▲
                              Cloud Run Job (Python) ◄── nightly Cloud Scheduler
```

- **Read path:** MCP client → Cloud Run (TypeScript) → BigQuery public dataset.
- **Write path:** nightly Cloud Run Job (Python) replays each source → raw event log → compiled, redacted, analytics-ready tables.
- **PII boundary:** personal data (contact names/emails/phones) is redacted before anything reaches the public, queryable dataset; the API service account can read *only* that dataset.

See [`docs/PRD.md`](docs/PRD.md), the [ADRs](docs/adr/), and the glossary in [`CONTEXT.md`](CONTEXT.md).

## Data sources

| Source | Records | Status / mechanism |
|--------|--------:|--------------------|
| **Find a Tender (FTS)** | ~109.6k | **Live** — OCDS 1.1.x (`ocds-h6vhtk`), windowed `updatedFrom` replay |
| **Contracts Finder** | ~546.6k | **Live** — OCDS (`ocds-b5fd17`) + bulk daily harvester, 2016→ |
| **Public Contracts Scotland** | ~8.3k | **Live** — Proactis API (`ocds-r6ebe6`) |
| **Sell2Wales** | ~2.1k | **Partial** — HTML scrape, 2013→ (`ocds-kuma6s`). Its entire OCDS API (collection, single-notice, bulk) is broken upstream — `nvarchar→float` SQL error — so coverage is limited to what the public archived search exposes (~120/yr). Above-threshold Welsh notices are captured in full via FTS. |
| **eTendersNI** | ~10.7k | **Snapshot** — HTML scrape, CAPTCHA solved by **Gemini**; refreshed manually from macOS (its WAF blocks Linux/cloud — see DEPLOYMENT.md) |

All five portals are ingested into one canonical OCDS model with **cross-source deduplication**
(`process_group`, blocking + fuzzy buyer/title/value matching). The nightly Cloud Run Job
refreshes the four non-CAPTCHA sources + dedup at 02:30 Europe/London; eTendersNI is a
periodic macOS refresh (`scripts/refresh_etendersni.sh`).

## Develop

```bash
# ingestion (Python)
python3 -m venv .venv && .venv/bin/pip install -e 'ingestion[dev]'
PYTHONPATH=ingestion/src .venv/bin/python -m pytest ingestion/tests -q
# validate against live FTS without touching the cloud:
PYTHONPATH=ingestion/src .venv/bin/python -m uk_tenders_ingest --source fts --mode backfill \
  --from 2026-05-01 --to 2026-05-31 --dry-run --max-releases 100

# API (TypeScript)
cd api && npm install && npm test && npm run build
GCP_PROJECT=govreposcrape node dist/index.js   # local server against BigQuery (needs ADC)
node test/smoke.mjs http://localhost:8080/mcp
```

## Deploy

See [DEPLOYMENT.md](DEPLOYMENT.md). In short: `./scripts/deploy_api.sh` (API) and
`./scripts/deploy_ingest.sh` (nightly job), or `terraform/` for the full IaC.

## License

Code: MIT. Data: Open Government Licence v3.0 (attribution preserved).
