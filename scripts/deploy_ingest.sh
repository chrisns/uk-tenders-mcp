#!/usr/bin/env bash
# Deploy the nightly ingestion: Cloud Run Job (Python) + Cloud Scheduler trigger.
# Ingestion SA can WRITE both datasets; nightly at 02:30 Europe/London.
#   PROJECT=govreposcrape REGION=europe-west2 ./scripts/deploy_ingest.sh
set -euo pipefail

PROJECT="${PROJECT:-govreposcrape}"
REGION="${REGION:-europe-west2}"
BQ_LOCATION="${BQ_LOCATION:-EU}"
REPO="uk-tenders"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/uk-tenders-ingest:latest"
SA="uk-tenders-ingest"; SA_EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"
SCHED_SA="uk-tenders-scheduler"; SCHED_EMAIL="${SCHED_SA}@${PROJECT}.iam.gserviceaccount.com"

echo ">> Artifact Registry repo"
gcloud artifacts repositories describe "${REPO}" --location "${REGION}" --project "${PROJECT}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${REPO}" --repository-format docker --location "${REGION}" --project "${PROJECT}"

echo ">> ingestion service account + dataset WRITER on both datasets"
gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${SA}" --project "${PROJECT}" --display-name "UK Tenders MCP ingestion"
gcloud projects add-iam-policy-binding "${PROJECT}" --member "serviceAccount:${SA_EMAIL}" \
  --role roles/bigquery.jobUser --condition=None --quiet >/dev/null
# Gemini (Vertex AI) reads the eTendersNI search CAPTCHA in the nightly run. No eTendersNI
# login is needed — the public search + notice pages are reachable with just a session cookie,
# so the only requirement is Vertex AI access for the captcha solve.
gcloud services enable aiplatform.googleapis.com --project "${PROJECT}" --quiet
gcloud projects add-iam-policy-binding "${PROJECT}" --member "serviceAccount:${SA_EMAIL}" \
  --role roles/aiplatform.user --condition=None --quiet >/dev/null
for DS in uk_tenders_raw uk_tenders_public; do
  TMP=$(mktemp); bq show --format=prettyjson "${PROJECT}:${DS}" > "${TMP}"
  python3 - "$TMP" "$SA_EMAIL" <<'PY'
import json,sys
p,sa=sys.argv[1],sys.argv[2]; d=json.load(open(p)); acc=d.get("access",[])
if not any(a.get("userByEmail")==sa and a.get("role")=="WRITER" for a in acc):
    acc.append({"role":"WRITER","userByEmail":sa}); d["access"]=acc; json.dump(d,open(p,"w"))
PY
  bq update --source "${TMP}" "${PROJECT}:${DS}" >/dev/null; rm -f "${TMP}"
done

echo ">> build ingestion image"
gcloud builds submit --project "${PROJECT}" --config cloudbuild-ingest.yaml --substitutions "_IMAGE=${IMAGE}" --quiet .

echo ">> create/update Cloud Run Job"
gcloud run jobs deploy uk-tenders-ingest \
  --image "${IMAGE}" --project "${PROJECT}" --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --set-env-vars "GCP_PROJECT=${PROJECT},BQ_LOCATION=${BQ_LOCATION},VERTEX_PROJECT=${PROJECT},VERTEX_LOCATION=${VERTEX_LOCATION:-us-central1}" \
  --max-retries 1 --task-timeout 3600 --quiet
# Args come from the image CMD (--source all --mode nightly --match); override per-execution if needed.

echo ">> scheduler service account + nightly trigger"
gcloud iam service-accounts describe "${SCHED_EMAIL}" --project "${PROJECT}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${SCHED_SA}" --project "${PROJECT}" --display-name "UK Tenders MCP scheduler"
gcloud projects add-iam-policy-binding "${PROJECT}" --member "serviceAccount:${SCHED_EMAIL}" \
  --role roles/run.invoker --condition=None --quiet >/dev/null
RUN_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/uk-tenders-ingest:run"
if gcloud scheduler jobs describe uk-tenders-nightly --location "${REGION}" --project "${PROJECT}" >/dev/null 2>&1; then
  CMD=update
else
  CMD=create
fi
gcloud scheduler jobs ${CMD} http uk-tenders-nightly --location "${REGION}" --project "${PROJECT}" \
  --schedule "30 2 * * *" --time-zone "Europe/London" \
  --uri "${RUN_URI}" --http-method POST \
  --oauth-service-account-email "${SCHED_EMAIL}" --quiet
echo ">> nightly ingestion deployed"
