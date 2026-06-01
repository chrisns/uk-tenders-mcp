export const meta = {
  name: 'contracts-finder-backfill',
  description: 'Resilient sharded backfill of UK Contracts Finder OCDS (2016-11 → 2026-05) into BigQuery, one month per shard (sequential — respects CF rate limits + avoids concurrent DML), then cross-source dedup.',
  phases: [
    { title: 'Backfill', detail: 'one isolated agent per month; partial/timeout shards are resumable (streaming + idempotent)' },
    { title: 'Dedup', detail: 'cross-source process_group matcher' },
  ],
}

const SHARD = {
  type: 'object',
  additionalProperties: false,
  properties: {
    window: { type: 'string' },
    status: { type: 'string', enum: ['success', 'partial_or_timeout', 'error'] },
    releases: { type: 'number' },
    loaded: { type: 'number' },
    processes: { type: 'number' },
    note: { type: 'string' },
  },
  required: ['window', 'status', 'note'],
}

const PY = '/Users/cns/httpdocs/cddo/find-tender-mcp/.venv/bin/python'
const SRC = '/Users/cns/httpdocs/cddo/find-tender-mcp/ingestion/src'

// Month windows 2016-11 .. 2026-05 inclusive (no Date() — unavailable in workflow scripts).
const months = []
let y = 2016
let m = 11
while (y < 2026 || (y === 2026 && m <= 5)) {
  months.push([y, m])
  m++
  if (m > 12) { m = 1; y++ }
}

phase('Backfill')
const results = []
for (const [yy, mm] of months) {
  const f = `${yy}-${String(mm).padStart(2, '0')}-01`
  const ny = mm === 12 ? yy + 1 : yy
  const nm = mm === 12 ? 1 : mm + 1
  const t = `${ny}-${String(nm).padStart(2, '0')}-01`
  const cmd =
    `GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONUNBUFFERED=1 PYTHONPATH=${SRC} ` +
    `${PY} -m uk_tenders_ingest --source contracts_finder --mode backfill --from ${f} --to ${t}`
  const r = await agent(
    `Run EXACTLY this one shell command, using a Bash timeout of 600000 ms (10 minutes). It loads one month of UK Contracts Finder procurement data into BigQuery; it streams per week and is idempotent, so any work done before a timeout is already saved.\n\n${cmd}\n\nThen report a JSON object: window="${f}"; pull releases/loaded/processes from the command's final JSON summary line if present; status="success" if it printed that final summary, "partial_or_timeout" if the Bash timed out, "error" (with a short message in note) if it failed for another reason. Do not run any other commands.`,
    { label: `cf:${f}`, phase: 'Backfill', schema: SHARD, agentType: 'general-purpose' },
  )
  results.push(r)
  if (r) log(`${f}: ${r.status} releases=${r.releases ?? '?'} loaded=${r.loaded ?? '?'} processes=${r.processes ?? '?'}`)
  else log(`${f}: (skipped/null)`)
}

phase('Dedup')
const match = await agent(
  `Run EXACTLY this command with a 600000 ms Bash timeout and report its final JSON summary line verbatim:\n\nGCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=${SRC} ${PY} -m uk_tenders_ingest.match`,
  { label: 'dedup', phase: 'Dedup', agentType: 'general-purpose' },
)

const ok = results.filter((r) => r && r.status === 'success').length
const partial = results.filter((r) => r && r.status === 'partial_or_timeout').map((r) => r.window)
const failed = results.filter((r) => r && r.status === 'error').map((r) => r.window)
return { months: months.length, success: ok, partial, failed, match }
