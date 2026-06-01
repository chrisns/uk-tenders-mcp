output "api_url" {
  value       = length(google_cloud_run_v2_service.api) > 0 ? google_cloud_run_v2_service.api[0].uri : "(set api_image to deploy)"
  description = "Cloud Run service URL; MCP endpoint is <url>/mcp"
}

output "api_service_account" {
  value = google_service_account.api.email
}

output "ingest_service_account" {
  value = google_service_account.ingest.email
}

output "raw_dataset" {
  value = google_bigquery_dataset.raw.dataset_id
}

output "public_dataset" {
  value = google_bigquery_dataset.public.dataset_id
}
