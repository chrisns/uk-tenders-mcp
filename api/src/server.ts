// Builds the MCP server and registers the UK Tenders tool surface (PRD §8.2).
// All analytics run over the public dataset; query_sql is read-only and byte-capped.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { config, tableRef } from "./lib/config.js";
import { runQuery, runQueryWithStats, plain } from "./lib/bigquery.js";
import { ok, toolError, guard, type ToolResult } from "./lib/errors.js";
import { shapeProcess, processCols, freshness, withFreshness, type ResultMode } from "./lib/format.js";
import { validateReadOnlySql } from "./lib/sqlguard.js";

const RESULT_MODE = z.enum(["minimal", "standard", "full"]).default("minimal");
const SOURCE = z.enum(["fts", "contracts_finder", "pcs", "sell2wales", "etendersni"]);

// ---- shared filter builder (parameterised; safe) ---------------------------
interface Filters {
  query?: string;
  cpv?: string;
  buyer?: string;
  supplier?: string;
  min_value?: number;
  max_value?: number;
  status?: string;
  stage?: string;
  regime?: string;
  source?: string;
  region?: string;
  published_from?: string;
  published_to?: string;
}

function buildWhere(f: Filters): { clause: string; params: Record<string, unknown> } {
  const conds: string[] = [];
  const params: Record<string, unknown> = {};
  if (f.query) {
    conds.push(`LOWER(IFNULL(title,'') || ' ' || IFNULL(description,'')) LIKE LOWER(@query)`);
    params.query = `%${f.query}%`;
  }
  if (f.cpv) {
    conds.push(`(cpv_division = @cpv OR @cpv IN UNNEST(cpv_codes) OR EXISTS (SELECT 1 FROM UNNEST(cpv_codes) c WHERE STARTS_WITH(c, @cpv)))`);
    params.cpv = f.cpv;
  }
  if (f.buyer) {
    conds.push(`LOWER(buyer_name) LIKE LOWER(@buyer)`);
    params.buyer = `%${f.buyer}%`;
  }
  if (f.supplier) {
    conds.push(`EXISTS (SELECT 1 FROM UNNEST(awards) a WHERE LOWER(a.supplier_name) LIKE LOWER(@supplier))`);
    params.supplier = `%${f.supplier}%`;
  }
  if (f.min_value !== undefined) {
    conds.push(`COALESCE(awarded_amount, value_amount) >= @min_value`);
    params.min_value = f.min_value;
  }
  if (f.max_value !== undefined) {
    conds.push(`COALESCE(awarded_amount, value_amount) <= @max_value`);
    params.max_value = f.max_value;
  }
  for (const k of ["status", "stage", "regime", "source"] as const) {
    if (f[k]) {
      conds.push(`${k} = @${k}`);
      params[k] = f[k];
    }
  }
  if (f.region) {
    conds.push(`LOWER(region) LIKE LOWER(@region)`);
    params.region = `%${f.region}%`;
  }
  if (f.published_from) {
    conds.push(`published_date >= TIMESTAMP(@published_from)`);
    params.published_from = f.published_from;
  }
  if (f.published_to) {
    conds.push(`published_date <= TIMESTAMP(@published_to)`);
    params.published_to = f.published_to;
  }
  const clause = conds.length ? `WHERE ${conds.join(" AND ")}` : "";
  return { clause, params };
}

const FILTER_SHAPE = {
  query: z.string().optional().describe("free-text match on title/description"),
  cpv: z.string().optional().describe("CPV code or 2-digit division prefix"),
  buyer: z.string().optional(),
  supplier: z.string().optional(),
  min_value: z.number().optional(),
  max_value: z.number().optional(),
  status: z.string().optional().describe("tender.status, e.g. active, complete, cancelled"),
  stage: z.enum(["planning", "tender", "award"]).optional(),
  regime: z.enum(["pca2023", "legacy", "scotland", "mixed"]).optional(),
  source: SOURCE.optional(),
  region: z.string().optional(),
  published_from: z.string().optional().describe("ISO date"),
  published_to: z.string().optional().describe("ISO date"),
};

// ---- aggregate dimension/metric allowlists (inlined safely) ----------------
const DIMENSION_SQL: Record<string, { expr: string; from: string }> = {
  buyer: { expr: "buyer_name", from: tableRef("compiled_process") },
  supplier: { expr: "a.supplier_name", from: `${tableRef("compiled_process")}, UNNEST(awards) a` },
  cpv_division: { expr: "cpv_division", from: tableRef("compiled_process") },
  cpv: { expr: "c", from: `${tableRef("compiled_process")}, UNNEST(cpv_codes) c` },
  stage: { expr: "stage", from: tableRef("compiled_process") },
  status: { expr: "status", from: tableRef("compiled_process") },
  regime: { expr: "regime", from: tableRef("compiled_process") },
  source: { expr: "source", from: tableRef("compiled_process") },
  region: { expr: "region", from: tableRef("compiled_process") },
  month: { expr: "FORMAT_TIMESTAMP('%Y-%m', published_date)", from: tableRef("compiled_process") },
  quarter: { expr: "FORMAT_TIMESTAMP('%Y-Q', published_date)", from: tableRef("compiled_process") },
  year: { expr: "FORMAT_TIMESTAMP('%Y', published_date)", from: tableRef("compiled_process") },
};

function valueExpr(dimension: string, valueField: string): string {
  if (dimension === "supplier") return "a.amount";
  return valueField === "estimated" ? "value_amount" : "awarded_amount";
}

// Explicit allowlist of metric → SQL (defence-in-depth beyond the zod enum; never inlines input).
const METRIC_FNS: Record<string, (v: string) => string> = {
  count: () => "COUNT(*)",
  sum: (v) => `SUM(${v})`,
  avg: (v) => `AVG(${v})`,
  min: (v) => `MIN(${v})`,
  max: (v) => `MAX(${v})`,
  median: (v) => `APPROX_QUANTILES(${v}, 2)[OFFSET(1)]`,
};
function metricExpr(metric: string, vexpr: string): string {
  const fn = METRIC_FNS[metric];
  if (!fn) throw new Error(`unknown metric '${metric}'`);
  return fn(vexpr);
}

const MAX_OFFSET = 100_000;
// Decode and bound a pagination cursor (prevents forged unbounded OFFSET → billable DoS).
function parseCursor(cursor?: string): number {
  if (!cursor) return 0;
  let decoded: string;
  try {
    decoded = Buffer.from(cursor, "base64").toString("utf8");
  } catch {
    throw new Error("invalid cursor");
  }
  const n = Number(decoded);
  if (!Number.isInteger(n) || n < 0 || n > MAX_OFFSET) throw new Error("invalid cursor");
  return n;
}

async function runAggregate(args: {
  metric: string;
  dimension: string;
  value_field?: string;
  limit?: number;
} & Filters): Promise<ToolResult> {
  const dim = DIMENSION_SQL[args.dimension];
  if (!dim) return toolError("INVALID_QUERY", `unknown dimension '${args.dimension}'`);
  const isValueMetric = args.metric !== "count";
  const vexpr = valueExpr(args.dimension, args.value_field ?? "awarded");
  const currencyCol = args.dimension === "supplier" ? "a.currency" : (args.value_field === "estimated" ? "value_currency" : "awarded_currency");
  const { clause, params } = buildWhere(args);
  const limit = Math.min(Math.max(args.limit ?? 50, 1), 1000);

  // Value metrics group by currency too — never aggregate across currencies (PRD §8.2).
  const selectCols = [`${dim.expr} AS dimension`];
  const groupCols = [dim.expr];
  if (isValueMetric) {
    selectCols.push(`${currencyCol} AS currency`);
    groupCols.push(currencyCol);
  }
  selectCols.push(`${metricExpr(args.metric, vexpr)} AS value`, `COUNT(*) AS n`);

  const sql = `SELECT ${selectCols.join(", ")}
    FROM ${dim.from}
    ${clause}
    GROUP BY ${groupCols.join(", ")}
    ORDER BY value DESC
    LIMIT ${limit}`;
  const rows = await runQuery(sql, { params });
  const shaped = rows.map((r) => ({
    dimension: r.dimension,
    ...(isValueMetric ? { currency: r.currency } : {}),
    value: plain(r.value),
    count: plain(r.n),
  }));
  const note = isValueMetric
    ? "Value = awarded contract value (not actual spend); grouped by currency — never summed across currencies."
    : undefined;
  return ok(withFreshness({ metric: args.metric, dimension: args.dimension, results: shaped, note }, await freshness()));
}

// ---------------------------------------------------------------- build server
export function buildServer(): McpServer {
  const server = new McpServer({ name: "uk-tenders-mcp", version: "0.1.0" });

  server.registerTool(
    "search_tenders",
    {
      title: "Search tenders",
      description:
        "Search UK public-procurement contracting processes by keyword, CPV, buyer, supplier, value, status, stage, regime, region, source and dates. Returns the official notice URL on every result. Aggregated/analytical questions should use aggregate_tenders or query_sql.",
      inputSchema: {
        ...FILTER_SHAPE,
        limit: z.number().int().min(1).max(config.maxLimit).default(config.defaultLimit),
        cursor: z.string().optional().describe("opaque pagination cursor from a previous call"),
        resultMode: RESULT_MODE,
      },
    },
    guard(async (args) => {
      let offset: number;
      try {
        offset = parseCursor(args.cursor);
      } catch {
        return toolError("INVALID_CURSOR", "Pagination cursor is invalid.", "Omit cursor to start from the first page.");
      }
      const { clause, params } = buildWhere(args);
      const limit = args.limit;
      const sql = `SELECT ${processCols(args.resultMode as ResultMode)} FROM ${tableRef("compiled_process")} ${clause}
        ORDER BY published_date DESC NULLS LAST LIMIT ${limit + 1} OFFSET ${offset}`;
      const rows = await runQuery(sql, { params });
      const hasMore = rows.length > limit;
      const page = rows.slice(0, limit).map((r) => shapeProcess(r, args.resultMode as ResultMode));
      const nextCursor = hasMore ? Buffer.from(String(offset + limit)).toString("base64") : null;
      return ok(withFreshness({ count: page.length, nextCursor, results: page }, await freshness()));
    }),
  );

  server.registerTool(
    "get_tender",
    {
      title: "Get a tender",
      description:
        "Fetch one contracting process by OCID or notice id, with its compiled current state, change timeline ('what changed'), and the official notice URL.",
      inputSchema: { id: z.string().describe("OCID (ocds-...) or notice id (nnnnnn-yyyy)"), resultMode: RESULT_MODE },
    },
    guard(async (args) => {
      // Two-step fetch. The notice-id form matches on an official_url suffix, which can
      // never block-prune — so resolve identity over three narrow columns first, then
      // read the wide row with equality predicates on the cluster keys. buyer_name is
      // included when present so the legacy (source, buyer_name, cpv_division) layout
      // prunes too, not just the (source, ocid) layout in schema.sql.
      const found = await runQuery<{ ocid: string; source: string; buyer_name: string | null }>(
        `SELECT ocid, source, buyer_name FROM ${tableRef("compiled_process")}
         WHERE ocid = @id OR official_url LIKE CONCAT('%/', @id) LIMIT 1`,
        { params: { id: args.id } },
      );
      if (!found.length) return toolError("NOT_FOUND", `no tender for id '${args.id}'`, "Try the OCID (ocds-h6vhtk-...) or the notice id (nnnnnn-yyyy).");
      const key = found[0];
      const conds = ["source = @source", "ocid = @ocid"];
      const params: Record<string, unknown> = { source: key.source, ocid: key.ocid };
      if (key.buyer_name != null) {
        conds.push("buyer_name = @buyer_name");
        params.buyer_name = key.buyer_name;
      }
      const rows = await runQuery(
        `SELECT ${processCols(args.resultMode as ResultMode)} FROM ${tableRef("compiled_process")}
         WHERE ${conds.join(" AND ")} LIMIT 1`,
        { params },
      );
      if (!rows.length) return toolError("NOT_FOUND", `no tender for id '${args.id}'`, "Try the OCID (ocds-h6vhtk-...) or the notice id (nnnnnn-yyyy).");
      const process = shapeProcess(rows[0], args.resultMode as ResultMode);
      const changes = await runQuery(
        `SELECT change_class, field_path, old_value, new_value, changed_at, official_url
         FROM ${tableRef("process_change")} WHERE source = @source AND ocid = @ocid ORDER BY changed_at`,
        { params: { source: key.source, ocid: key.ocid } },
      );
      return ok(withFreshness({ process, change_timeline: changes.map((c) => ({ ...c, changed_at: plain(c.changed_at) })) }, await freshness()));
    }),
  );

  server.registerTool(
    "list_updates",
    {
      title: "List updates",
      description:
        "List changes to tenders since a timestamp (as of the last nightly ingest) — deadline changes, value changes, cancellations, new awards. Pull-based; not real-time alerting.",
      inputSchema: {
        since: z.string().describe("ISO timestamp"),
        source: SOURCE.optional(),
        change_class: z.enum(["deadline_changed", "value_changed", "cancelled", "new_award"]).optional(),
        limit: z.number().int().min(1).max(500).default(100),
      },
    },
    guard(async (args) => {
      const conds = ["changed_at >= TIMESTAMP(@since)"];
      const params: Record<string, unknown> = { since: args.since };
      if (args.source) { conds.push("source = @source"); params.source = args.source; }
      if (args.change_class) { conds.push("change_class = @cc"); params.cc = args.change_class; }
      const rows = await runQuery(
        `SELECT ocid, source, change_class, field_path, old_value, new_value, changed_at, official_url
         FROM ${tableRef("process_change")} WHERE ${conds.join(" AND ")}
         ORDER BY changed_at DESC LIMIT ${args.limit}`,
        { params },
      );
      return ok(withFreshness({ count: rows.length, updates: rows.map((r) => ({ ...r, changed_at: plain(r.changed_at) })) }, await freshness()));
    }),
  );

  server.registerTool(
    "aggregate_tenders",
    {
      title: "Aggregate tenders",
      description:
        "Flexible analytics: a metric (count, or sum/avg/min/max/median of value) grouped by a dimension (buyer, supplier, cpv_division, cpv, stage, status, regime, source, region, month, quarter, year), with the same filters as search_tenders. Value = AWARDED contract value (not actual spend); always grouped by currency. median is APPROX_QUANTILES (approximate).",
      inputSchema: {
        metric: z.enum(["count", "sum", "avg", "min", "max", "median"]).default("count"),
        dimension: z.enum(Object.keys(DIMENSION_SQL) as [string, ...string[]]),
        value_field: z.enum(["awarded", "estimated"]).default("awarded"),
        limit: z.number().int().min(1).max(1000).default(50),
        ...FILTER_SHAPE,
      },
    },
    guard(async (args) => runAggregate(args)),
  );

  // shortcuts over the aggregate engine
  server.registerTool(
    "top_suppliers",
    { title: "Top suppliers", description: "Suppliers ranked by total awarded contract value (not actual spend).", inputSchema: { limit: z.number().int().min(1).max(500).default(20), ...FILTER_SHAPE } },
    guard(async (args) => runAggregate({ ...args, metric: "sum", dimension: "supplier", value_field: "awarded" })),
  );
  server.registerTool(
    "awarded_value_by_buyer",
    { title: "Awarded value by buyer", description: "Buyers ranked by total awarded contract value (not actual payments/spend).", inputSchema: { limit: z.number().int().min(1).max(500).default(20), ...FILTER_SHAPE } },
    guard(async (args) => runAggregate({ ...args, metric: "sum", dimension: "buyer", value_field: "awarded" })),
  );
  server.registerTool(
    "awards_over_time",
    { title: "Awards over time", description: "Count of processes by month.", inputSchema: { limit: z.number().int().min(1).max(1000).default(60), ...FILTER_SHAPE } },
    guard(async (args) => runAggregate({ ...args, metric: "count", dimension: "month" })),
  );
  server.registerTool(
    "cpv_breakdown",
    { title: "CPV breakdown", description: "Count of processes by CPV division.", inputSchema: { limit: z.number().int().min(1).max(100).default(45), ...FILTER_SHAPE } },
    guard(async (args) => runAggregate({ ...args, metric: "count", dimension: "cpv_division" })),
  );

  server.registerTool(
    "query_sql",
    {
      title: "Read-only SQL",
      description:
        `Run a read-only BigQuery Standard SQL SELECT over the public dataset for open-ended analytics. Single statement, SELECT/WITH only. Capped at ${config.maxBytesHuman} scanned per query (call get_schema for tables/columns).`,
      inputSchema: { sql: z.string().describe("a single read-only SELECT/WITH statement") },
    },
    guard(async (args) => {
      const g = validateReadOnlySql(args.sql);
      if (!g.ok) return toolError(g.code, g.message);
      // Enforced at runtime (maximumBytesBilled), not via a dry-run pre-check: dry-run
      // estimates ignore cluster pruning, so a pre-check rejects cheap point lookups
      // (e.g. WHERE source/ocid) that the runtime bills at a fraction of the cap.
      // Over-cap queries are cancelled unbilled and map to QUERY_TOO_LARGE in guard().
      const { rows, totalBytesProcessed } = await runQueryWithStats(g.sql);
      const coerced = rows.slice(0, 1000).map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, plain(v)])));
      return ok({ bytes_processed: totalBytesProcessed, row_count: rows.length, rows: coerced });
    }),
  );

  server.registerTool(
    "get_schema",
    {
      title: "Get schema",
      description: "List queryable tables and columns in the public dataset, and the per-query byte cap, for use with query_sql.",
      inputSchema: {},
    },
    guard(async () => {
      const rows = await runQuery(
        `SELECT table_name, column_name, data_type FROM \`${config.project}.${config.publicDataset}.INFORMATION_SCHEMA.COLUMNS\`
         ORDER BY table_name, ordinal_position`,
      );
      const tables: Record<string, { column: unknown; type: unknown }[]> = {};
      for (const r of rows) {
        (tables[r.table_name as string] ??= []).push({ column: r.column_name, type: r.data_type });
      }
      return ok({ dataset: `${config.project}.${config.publicDataset}`, max_bytes_per_query: config.maxBytesHuman, tables });
    }),
  );

  server.registerTool(
    "get_status",
    {
      title: "Get status",
      description: "Per-source freshness, coverage and health (green/amber/red) of the index.",
      inputSchema: {},
    },
    guard(async () => ok(await freshness())),
  );

  return server;
}
