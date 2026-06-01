// Runtime configuration (env-overridable so the same build runs locally and on Cloud Run).

export const config = {
  project: process.env.GCP_PROJECT || "govreposcrape",
  publicDataset: process.env.BQ_PUBLIC_DATASET || "uk_tenders_public",
  location: process.env.BQ_LOCATION || "EU",
  // Hard per-query scan cap (ADR-0003). Default 2 GiB.
  maxBytesBilled: process.env.MAX_BYTES_BILLED || String(2 * 1024 * 1024 * 1024),
  // Per-query wall-clock cap (ms) — bounds a slow-but-under-byte-cap query on the single instance.
  jobTimeoutMs: process.env.JOB_TIMEOUT_MS || "30000",
  port: parseInt(process.env.PORT || "8080", 10),
  defaultLimit: 20,
  maxLimit: 100,
  // Surfaced in tool descriptions so the assistant can right-size queries.
  get maxBytesHuman() {
    return `${(Number(this.maxBytesBilled) / 1024 / 1024 / 1024).toFixed(1)} GiB`;
  },
} as const;

export const SOURCES = ["fts", "contracts_finder", "pcs", "sell2wales", "etendersni"] as const;
export type Source = (typeof SOURCES)[number];

export function tableRef(table: string): string {
  return `\`${config.project}.${config.publicDataset}.${table}\``;
}
