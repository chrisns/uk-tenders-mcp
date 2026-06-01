#!/usr/bin/env bash
# Deploy the UK Tenders MCP API to Cloud Run with a least-privilege read-only
# service account (ADR-0003). Idempotent. Reads-only SA sees ONLY the public dataset.
#
#   PROJECT=govreposcrape REGION=europe-west1 ./scripts/deploy_api.sh
set -euo pipefail

PROJECT="${PROJECT:-govreposcrape}"
REGION="${REGION:-europe-west1}"
PUBLIC_DATASET="${PUBLIC_DATASET:-uk_tenders_public}"
BQ_LOCATION="${BQ_LOCATION:-EU}"
SERVICE="${SERVICE:-uk-tenders-mcp}"
SA="uk-tenders-api"
SA_EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"
MAX_BYTES="${MAX_BYTES:-2147483648}" # 2 GiB

echo ">> ensuring read-only service account ${SA_EMAIL}"
gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${SA}" --project "${PROJECT}" \
       --display-name "UK Tenders MCP API (read-only)"

echo ">> granting bigquery.jobUser (project) + dataViewer (public dataset only)"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/bigquery.jobUser \
  --condition=None --quiet >/dev/null

# dataset-scoped dataViewer: the SA may read ONLY uk_tenders_public (never the raw/PII dataset)
TMP=$(mktemp)
bq show --format=prettyjson "${PROJECT}:${PUBLIC_DATASET}" > "${TMP}"
python3 - "$TMP" "$SA_EMAIL" <<'PY'
import json, sys
path, sa = sys.argv[1], sys.argv[2]
d = json.load(open(path))
acc = d.get("access", [])
if not any(a.get("userByEmail")==sa and a.get("role")=="READER" for a in acc):
    acc.append({"role":"READER","userByEmail":sa})
    d["access"]=acc
    json.dump(d, open(path,"w"))
    print("added")
else:
    print("present")
PY
bq update --source "${TMP}" "${PROJECT}:${PUBLIC_DATASET}" >/dev/null
rm -f "${TMP}"

echo ">> deploying Cloud Run service ${SERVICE} (region ${REGION})"
gcloud run deploy "${SERVICE}" \
  --source ./api \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --allow-unauthenticated \
  --memory 512Mi --cpu 1 --min-instances 0 --max-instances 1 --session-affinity --timeout 60 \
  --set-env-vars "GCP_PROJECT=${PROJECT},BQ_PUBLIC_DATASET=${PUBLIC_DATASET},BQ_LOCATION=${BQ_LOCATION},MAX_BYTES_BILLED=${MAX_BYTES}" \
  --quiet

URL=$(gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')
echo ">> deployed: ${URL}"
echo ">> MCP endpoint: ${URL}/mcp"
