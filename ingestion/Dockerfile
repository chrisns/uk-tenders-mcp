# UK Tenders MCP — ingestion image (Cloud Run Job). Build context: repo root.
#   docker build -f ingestion/Dockerfile -t uk-tenders-ingest .
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203
WORKDIR /app
# Major-version-bounded pins (no unbounded floors). google-genai: Gemini (Vertex AI) reads the
# eTendersNI CAPTCHA. curl_cffi: ships a patched libcurl for Chrome TLS impersonation — a
# security-sensitive native dep; pinned + bounded so a future major isn't pulled silently.
RUN pip install --no-cache-dir "google-cloud-bigquery>=3.25,<4" "requests>=2.31,<3" \
    "ocdsmerge>=0.6,<1" "google-genai>=1.0,<2" "curl_cffi>=0.7,<1"
COPY ingestion/pyproject.toml ./ingestion/pyproject.toml
COPY ingestion/src ./ingestion/src
COPY sql ./sql
ENV PYTHONPATH=/app/ingestion/src
# Run as a non-root user (defence-in-depth).
RUN useradd -m -u 10001 app && chown -R app /app
USER app
# Default: nightly run. Override args in the Cloud Run Job execution.
ENTRYPOINT ["python", "-m", "uk_tenders_ingest"]
# Nightly (GCP): ingest the 4 sources reachable from cloud, then cross-source dedup.
# eTendersNI is excluded — its anti-bot blocks datacenter IPs, so it's refreshed manually from
# macOS (scripts/refresh_etendersni.sh; see DEPLOYMENT.md). Override args per-execution as needed.
CMD ["--source", "all", "--exclude", "etendersni", "--mode", "nightly", "--match"]
