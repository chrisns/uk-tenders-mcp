export const meta = {
  name: 'cf-parallel-harvester-rawload',
  description: 'Parallel append-only raw load of the full Contracts Finder bulk-harvester history (2016-11→2026-05) into BigQuery, sharded by 2-month windows. Concurrency-safe (WRITE_APPEND, no compile/DML). Compile + dedup run separately afterwards.',
  phases: [{ title: 'Raw-load', detail: 'parallel 2-month shards via the bulk harvester' }],
}

const PY = '/Users/cns/httpdocs/cddo/find-tender-mcp/.venv/bin/python'
const SRC = '/Users/cns/httpdocs/cddo/find-tender-mcp/ingestion/src'
const SCRIPT = '/Users/cns/httpdocs/cddo/find-tender-mcp/scripts/cf_harvester_rawload.py'

// 2-month shards 2016-11 .. 2026-05 (no Date() in workflow scripts)
const shards = []
let y = 2016
let m = 11
while (y < 2026 || (y === 2026 && m <= 5)) {
  const f = `${y}-${String(m).padStart(2, '0')}-01`
  let ny = y
  let nm = m + 2
  if (nm > 12) { nm -= 12; ny += 1 }
  const t = `${ny}-${String(nm).padStart(2, '0')}-01`
  shards.push([f, t])
  y = ny
  m = nm
}

phase('Raw-load')
log(`launching ${shards.length} parallel 2-month raw-load shards`)

const results = await parallel(
  shards.map(([f, t]) => () =>
    agent(
      `Run EXACTLY this one shell command, using a Bash timeout of 600000 ms (10 minutes). It append-loads ~2 months of UK Contracts Finder bulk-harvester data into BigQuery — concurrency-safe, no compile. Command:\n\n` +
        `GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=${SRC} ${PY} ${SCRIPT} ${f} ${t}\n\n` +
        `Report ONLY its final "RAWLOAD ${f}..${t} seen=… appended=…" line, or the error text if it failed/timed out (partial appends are fine — it's resumable). Do not run any other command.`,
      { label: `raw:${f}`, phase: 'Raw-load', agentType: 'general-purpose' },
    ),
  ),
)

const ok = results.filter(Boolean)
log(`raw-load shards returned: ${ok.length}/${shards.length}`)
return { shards: shards.length, returned: ok.length, results: ok }
