# Deployment

UK Tenders MCP is a TypeScript MCP API on **Cloud Run** over a **BigQuery** index, fed by a
**Python** ingestion job (Cloud Run Job + nightly Cloud Scheduler). See the ADRs in `docs/adr/`.

## Where it runs

The dedicated home is the **`uk-tenders-mcp`** GCP project. It currently deploys into the
already-billed **`govreposcrape`** project because the billing account is at its 10-project
cap, so `uk-tenders-mcp` cannot have billing enabled (see below). Everything is namespaced
(`uk_tenders_raw` / `uk_tenders_public` datasets, `uk-tenders-mcp` Cloud Run service,
`uk-tenders-*` service accounts) and parameterised by `GCP_PROJECT`, so migrating to the
dedicated project once a billing slot is freed is a one-variable change.

> To use the dedicated project: free a billing slot (unlink an unused project, or request a
> billing-account project-quota increase), then `gcloud billing projects link uk-tenders-mcp
> --billing-account=<ACCT>` and set `PROJECT=uk-tenders-mcp` below.

## Architecture

```
FTS / (CF / PCS / Sell2Wales / eTendersNI) ──Python adapters──► GCS raw archive (PII)
                                                                      │
                          uk_tenders_raw  (BigQuery, WRITE, PII) ◄────┘
                             release_event_log → compiled_process (redacted)
                                                → process_change, process_group, source_status
                                                       │ redact + replace
                          uk_tenders_public (BigQuery, READ, no PII)
                                                       ▲
              Cloud Run: uk-tenders-mcp (TS) ──read-only SA──┘ ──MCP /mcp──► AI assistants
              Cloud Run Job: uk-tenders-ingest (Python) ◄── nightly Cloud Scheduler
```

## Prerequisites

- `gcloud` authenticated; ADC for local runs (`gcloud auth application-default login`).
- Node 22+, Python 3.11+, Docker, Terraform ≥ 1.5.
- Target project with billing + these APIs: bigquery, run, cloudscheduler, storage, cloudbuild, artifactregistry, iam.

## 1. Datasets + first data load

The ingestion job's `--bootstrap` creates datasets and tables from `sql/schema.sql`, then loads.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ingestion
# bounded historical backfill (full backfill: drop --max-releases, widen the window)
GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src \
  .venv/bin/python -m uk_tenders_ingest --source fts --mode backfill \
  --from 2021-01-01 --to 2026-05-31 --bootstrap
```

Multi-source: `--source all` covers all five sources; `--exclude etendersni` is used in the
cloud nightly (its WAF blocks datacenter IPs — eTendersNI is refreshed from macOS instead,
and Sell2Wales is HTML-scraped with a self-healing API gate). `--match` runs cross-source
deduplication afterwards:

```bash
GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src \
  .venv/bin/python -m uk_tenders_ingest --source all --mode nightly --match
# or one source: --source contracts_finder --mode backfill --from 2026-05-01 --to 2026-05-31
# cross-source dedup only:  python -m uk_tenders_ingest.match
```

Validate without touching the cloud:

```bash
PYTHONPATH=ingestion/src .venv/bin/python -m uk_tenders_ingest \
  --source fts --mode backfill --from 2026-05-01 --to 2026-05-31 --dry-run --max-releases 100
```

## 2. Deploy the API (Cloud Run)

Creates a least-privilege read-only service account (`dataViewer` on the public dataset only,
`jobUser` on the project), builds via Cloud Build, deploys public + single-instance.

The API runs in **europe-west1** (default) — **not** europe-west2 — because Cloud Run **domain
mappings aren't supported in europe-west2** (see *Custom domain* below). The ingest job has no
domain and stays in europe-west2.

```bash
PROJECT=govreposcrape REGION=europe-west1 ./scripts/deploy_api.sh
# → prints the *.run.app URL; the stable public endpoint is https://tenders.run.cns.me/mcp
```

### Custom domain — `https://tenders.run.cns.me/mcp`

The MCP is served at a stable custom domain via a Cloud Run **domain mapping**, so the
`*.a.run.app` hash never leaks into client configs. A wildcard `*.run.cns.me →
ghs.googlehosted.com` CNAME already exists and `cns.me` is a verified domain on the deploying
account, so **no per-service DNS record is needed** — just create the mapping once:

```bash
gcloud beta run domain-mappings create --service=uk-tenders-mcp \
  --domain=tenders.run.cns.me --region=europe-west1 --project=govreposcrape
```

Google then provisions the managed TLS cert automatically (~15 min – a few hours); it's live when
`curl https://tenders.run.cns.me/health` returns 200. To change the subdomain, recreate the
mapping with a different `--domain` (the wildcard covers any `*.run.cns.me`).

## 3. Nightly ingestion (Cloud Run Job + Scheduler)

```bash
PROJECT=govreposcrape REGION=europe-west2 ./scripts/deploy_ingest.sh
```

The nightly job runs `--source all --exclude etendersni` (4 sources + cross-source dedup).
eTendersNI is excluded because its F5 WAF fingerprints the OS/TCP stack and rejects Linux
(GCP/containers) regardless of IP, TLS, cookies, or a correct Gemini-solved CAPTCHA — only a
macOS-native client passes. Its CAPTCHA is solved by Gemini (Vertex AI), so the *mechanism*
works anywhere; only the WAF's OS fingerprinting blocks the cloud.

### Sell2Wales — upstream API broken (blocked, self-healing armed)

Sell2Wales' entire OCDS API (by-date collection, single-notice, and bulk download — incl.
download-by-OCID, verified in a real browser) fails with a server-side `nvarchar→float` SQL
error. **The Welsh Government has confirmed the fault with no ETA on a fix** (email, 2026-06).
The only working endpoint is the public HTML search, which is recent-sorted, date-leaky, and
caps at ~2,059 distinct notices (~1.2% of the ~165k `ocds-kuma6s` space). Internet Archive has
no crawl of the notice pages. So ~163k sub-threshold notices are **unretrievable by any client**
until the upstream API is repaired (above-threshold Welsh procurement is captured in full via
FTS regardless).

`scrape_ingest._sell2wales_api_healthy()` probes the API each nightly run: while broken it uses
the HTML-scrape fallback; **the moment it returns valid OCDS it auto-switches to the API and
backfills full history** (idempotent) — no manual action. Redeploy (`deploy_ingest.sh`) to
activate the gate in the cloud job.

### eTendersNI (manual refresh, run on macOS)

The ~10.6k historical CfTs are loaded. To refresh from a Mac (where the WAF accepts the
native stack):

```bash
./scripts/refresh_etendersni.sh                 # this year .. today
./scripts/refresh_etendersni.sh 2004-01-01      # full re-scrape
```

Loads are idempotent (delete-by-ocid), so re-running is safe; Gemini solves the CAPTCHA via
your local gcloud ADC. (A keyless k8s/WIF CronJob was prototyped but abandoned: eTendersNI's
WAF fingerprints the OS/TCP stack and blocks Linux regardless of a correct CAPTCHA, so the
refresh must run from macOS. If CPD ever provides a feed or whitelists a host, automate then.)

## Infrastructure as code (Terraform)

`terraform/` is the canonical, reproducible definition (datasets, IAM with the PII boundary,
GCS, Cloud Run service + job, nightly Scheduler). Tables are created by the ingestion
`--bootstrap` step (single source of truth = `sql/schema.sql`), not duplicated in HCL.

```bash
cd terraform
terraform init
terraform apply -var project=govreposcrape -var api_image=<IMAGE> -var ingest_image=<IMAGE>
```

## Verify

```bash
curl -s https://<url>/health
node api/test/smoke.mjs https://<url>/mcp     # lists tools, runs real queries
claude mcp add --transport http uk-tenders https://<url>/mcp
```

## Cost controls (PRD §10.2)

- Per-query `MAX_BYTES_BILLED` cap (default 2 GiB) on the API.
- Scale-to-zero Cloud Run, single instance.
- Set a **daily BigQuery spend cap + budget alert** on the project before opening up traffic.

## Public MCP endpoint — abuse / cost protections

The MCP is public + unauthenticated (ADR-0003), so the API enforces (in code, no infra step):
- **Per-IP rate limit** — 60 req/min/IP on `/mcp` (`RATE_LIMIT_PER_MIN` env to tune); `api/src/index.ts`.
- **Per-query BigQuery cost cap** — `maximumBytesBilled` = 2 GiB on every query **and** the dry-run
  (`MAX_BYTES_BILLED` env); a single query can't scan more. `api/src/lib/bigquery.ts` + `config.ts`.
- **Per-query wall-clock cap** — 30s `jobTimeoutMs` (`JOB_TIMEOUT_MS` env).
- **Result cap** — `maxResults: 1000` + `autoPaginate: false` so a fat result set can't OOM the
  single 512 Mi instance.
- **Request body cap** — 256 KiB; **errors are sanitised** (no raw BigQuery text → no schema/project
  enumeration); `api/src/lib/errors.ts`.

Belt-and-braces (recommended, operator step — a project-level daily ceiling against distributed
abuse, since the per-query cap is per-request): set a BigQuery daily custom quota in the console
(IAM & Admin → Quotas → BigQuery API → "Query usage per day") or:

```bash
gcloud alpha services quota update --service=bigquery.googleapis.com \
  --consumer=projects/PROJECT --metric=bigquery.googleapis.com/quota/query/usage \
  --unit=1/d/{project} --value=<bytes-per-day>   # e.g. 1099511627776 = 1 TiB/day
```
