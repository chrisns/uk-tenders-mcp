#!/usr/bin/env bash
# Manual eTendersNI refresh — RUN ON macOS.
#
# eTendersNI's F5 WAF fingerprints the OS/TCP stack: it accepts macOS but rejects Linux
# (containers/servers), independent of IP, TLS, cookies, or a correct CAPTCHA. So the nightly
# can't run on GCP/k8s — this script re-scrapes from your Mac, where it works. Gemini solves
# the CAPTCHA via your gcloud ADC; loads are idempotent (delete-by-ocid), so re-running is safe.
#
#   ./scripts/refresh_etendersni.sh [FROM] [TO]
# FROM/TO are ISO dates (default: this calendar year .. today). For a full re-scrape use 2004-01-01.
set -euo pipefail
cd "$(dirname "$0")/.."

FROM="${1:-$(date +%Y)-01-01}"
TO="${2:-$(date +%Y-%m-%d)}"

echo ">> eTendersNI refresh ${FROM} .. ${TO} (macOS, Gemini CAPTCHA via ADC)"
GCP_PROJECT=govreposcrape BQ_LOCATION=EU VERTEX_PROJECT=govreposcrape VERTEX_LOCATION=us-central1 \
  PYTHONPATH=ingestion/src .venv/bin/python -m uk_tenders_ingest \
  --source etendersni --mode backfill --from "${FROM}" --to "${TO}" --match
