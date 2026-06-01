# Public, unauthenticated MCP; read-only SQL secured by IAM, not by query parsing

**Status:** accepted

The MCP server is **public and unauthenticated** (parity with govreposcrape; all data is Open Government Licence v3.0). It exposes parameterised tools — `search_tenders`, `get_tender`, `list_updates`, the flexible `aggregate_tenders` plus named shortcuts (`top_suppliers`, `awarded_value_by_buyer`, `awards_over_time`, `cpv_breakdown`), and `get_status`/`get_schema` — **and** a raw **read-only SQL** tool over BigQuery.

The SQL tool's safety rests on **infrastructure, not parsing**: a least-privilege BigQuery service account holding only `dataViewer` + `jobUser` on the **public/redacted** read dataset (`uk_tenders_public`, see [ADR-0001](0001-bigquery-index-with-gcs-raw-and-star-schema.md)), plus a per-query `maximum_bytes_billed` cap that the tool dry-run-estimates against and refuses up front.

**Personal data is removed before the public surface.** The public read dataset is redacted (no `contactPoint` personal fields, etc., per the PRD Data Protection section); `query_sql` and the `full` resultMode operate **only** over this dataset. Raw releases with PII live solely in the access-controlled `uk_tenders_raw` tier, which the public API cannot reach. A malicious or malformed query *physically cannot* mutate data or exceed the byte budget. Cost and abuse are further contained by per-IP/connection rate limiting, statement timeouts, and a daily project-level BigQuery spend cap with alerting.

## Considered options

- **IAM-secured read-only SQL (chosen)** — maximal analytical power with safety guaranteed by the platform, not by fallible SQL sanitising.
- **App-level SQL parsing/whitelisting** — rejected: brittle and easy to get subtly wrong.
- **API-key gate on everything** — rejected: friction against the public-good aim and govreposcrape parity.
- **Parameterised tools only (no SQL)** — rejected: cannot serve open-ended analytics, which is the product's primary purpose.

## Consequences

The read dataset must be strictly separated from any table the ingestion job writes with elevated rights. The byte cap is a hard product constraint surfaced in the SQL tool's description so the assistant can right-size queries. Rate-limit and spend-cap thresholds need tuning post-launch.
