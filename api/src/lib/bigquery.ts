// Read-only BigQuery access. In production the API's service account holds only
// dataViewer + jobUser on the public dataset (ADR-0003); the byte cap is the cost backstop.

import { BigQuery } from "@google-cloud/bigquery";
import { config } from "./config.js";

const bq = new BigQuery({ projectId: config.project, location: config.location });

export interface QueryOpts {
  params?: Record<string, unknown>;
  types?: Record<string, string>;
  maxBytes?: string;
  maxResults?: number;
}

export async function runQuery<T = Record<string, unknown>>(
  sql: string,
  opts: QueryOpts = {},
): Promise<T[]> {
  const [rows] = await bq.query(
    {
      query: sql,
      params: opts.params,
      types: opts.types,
      location: config.location,
      maximumBytesBilled: opts.maxBytes ?? config.maxBytesBilled,
      // Cap rows fetched into memory (single 512Mi instance) + bound wall-clock.
      maxResults: opts.maxResults ?? 1000,
      jobTimeoutMs: Number(config.jobTimeoutMs),
    },
    // Don't auto-page the whole result set before the handler truncates.
    { autoPaginate: false },
  );
  return rows as T[];
}

// Estimate scanned bytes without running the query (used to pre-empt QUERY_TOO_LARGE).
export async function dryRunBytes(
  sql: string,
  params?: Record<string, unknown>,
): Promise<number> {
  const [job] = await bq.createQueryJob({
    query: sql,
    params,
    location: config.location,
    dryRun: true,
    maximumBytesBilled: config.maxBytesBilled,
  });
  const bytes = (job.metadata?.statistics as { totalBytesProcessed?: string } | undefined)
    ?.totalBytesProcessed;
  return bytes ? Number(bytes) : 0;
}

// Coerce BigQuery wrapper types (BigQueryTimestamp/Date, Big numerics) to plain JSON.
export function plain(value: unknown): unknown {
  if (value === null || value === undefined) return value;
  // Only unwrap genuine BigQuery wrapper types: an object whose ONLY key is `value`.
  if (typeof value === "object" && !Array.isArray(value)) {
    const keys = Object.keys(value as Record<string, unknown>);
    if (keys.length === 1 && keys[0] === "value") {
      return (value as { value: unknown }).value;
    }
  }
  return value;
}
