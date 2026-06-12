// Result shaping (PRD §8.3) and the per-source freshness envelope (PRD §8.4).

import { runQuery, plain } from "./bigquery.js";
import { tableRef } from "./config.js";

export type ResultMode = "minimal" | "standard" | "full";

// Column projection per result mode. Selecting only what the mode emits is what
// keeps row reads inside the byte cap: compiled_json dwarfs every other column
// combined, and only `full` reads it. Kept beside shapeProcess so the projection
// and the shaper cannot drift apart (pinned by a unit test).
const COLS_MINIMAL = [
  "ocid", "source", "title", "buyer_name", "status", "stage",
  "value_amount", "value_currency", "awarded_amount", "awarded_currency",
  "published_date", "tender_end_date", "official_url",
];
const COLS_STANDARD = [
  ...COLS_MINIMAL,
  "description", "regime", "main_category", "cpv_codes", "cpv_division",
  "region", "process_group_id", "last_updated", "awards", "parties",
];
const COLS_FULL = [...COLS_STANDARD, "compiled_json"];

export function processCols(mode: ResultMode): string {
  const cols = mode === "full" ? COLS_FULL : mode === "standard" ? COLS_STANDARD : COLS_MINIMAL;
  return cols.join(", ");
}

function p(v: unknown): unknown {
  return plain(v);
}

export function shapeProcess(row: Record<string, unknown>, mode: ResultMode): Record<string, unknown> {
  const minimal = {
    ocid: row.ocid,
    source: row.source,
    title: row.title,
    buyer_name: row.buyer_name,
    status: row.status,
    stage: row.stage,
    value_amount: p(row.value_amount),
    value_currency: row.value_currency,
    awarded_amount: p(row.awarded_amount),
    awarded_currency: row.awarded_currency,
    published_date: p(row.published_date),
    tender_end_date: p(row.tender_end_date),
    official_url: row.official_url,
  };
  if (mode === "minimal") return minimal;

  const awards = Array.isArray(row.awards) ? row.awards : [];
  const standard = {
    ...minimal,
    description: row.description,
    regime: row.regime,
    main_category: row.main_category,
    cpv_codes: row.cpv_codes,
    cpv_division: row.cpv_division,
    region: row.region,
    process_group_id: row.process_group_id,
    last_updated: p(row.last_updated),
    awards: awards.map((a) => coerceAward(a as Record<string, unknown>)),
    parties: row.parties,
  };
  if (mode === "standard") return standard;

  let compiled: unknown = null;
  if (typeof row.compiled_json === "string") {
    try {
      compiled = JSON.parse(row.compiled_json);
    } catch {
      compiled = null;
    }
  }
  return { ...standard, compiled };
}

function coerceAward(a: Record<string, unknown>): Record<string, unknown> {
  return { ...a, amount: p(a.amount), date: p(a.date) };
}

// ----------------------------------------------------------------- freshness

interface SourceStatus {
  source: string;
  last_success_at: unknown;
  last_run_status: string | null;
  health: string | null;
  coverage_from: unknown;
  coverage_to: unknown;
  releases_total: number | null;
}

let cache: { at: number; data: unknown } | null = null;
const TTL_MS = 60_000;

export async function freshness(): Promise<unknown> {
  if (cache && Date.now() - cache.at < TTL_MS) return cache.data;
  let rows: SourceStatus[] = [];
  try {
    rows = await runQuery<SourceStatus>(
      `SELECT source, last_success_at, last_run_status, health, coverage_from, coverage_to, releases_total
       FROM ${tableRef("source_status")} ORDER BY source`,
    );
  } catch {
    rows = [];
  }
  const sources = rows.map((r) => ({
    source: r.source,
    health: r.health,
    last_success_at: p(r.last_success_at),
    last_run_status: r.last_run_status,
    coverage_from: p(r.coverage_from),
    coverage_to: p(r.coverage_to),
    releases_total: r.releases_total,
  }));
  const worst = sources.reduce<unknown>((acc, s) => {
    const v = p(s.last_success_at);
    if (!acc) return v;
    return v && String(v) < String(acc) ? v : acc;
  }, null);
  const degraded = sources.filter((s) => s.health !== "green").map((s) => s.source);
  const data = {
    data_current_as_of: worst,
    degraded_or_excluded_sources: degraded,
    sources,
    note: "Mirror of source data under OGL v3.0; verify critical details on the official notice.",
  };
  cache = { at: Date.now(), data };
  return data;
}

export function withFreshness<T extends Record<string, unknown>>(payload: T, fresh: unknown): T & { freshness: unknown } {
  return { ...payload, freshness: fresh };
}
