variable "project" {
  type        = string
  description = "GCP project id. Dedicated home is uk-tenders-mcp; currently deployed into govreposcrape (billing-cap, see DEPLOYMENT.md)."
  default     = "govreposcrape"
}

variable "region" {
  type = string
  # Must be a Cloud Run domain-mapping-supported region for the custom domain to work
  # (europe-west2 is NOT supported). See DEPLOYMENT.md → Custom domain.
  default = "europe-west1"
}

variable "api_domain" {
  type        = string
  description = "Stable custom domain mapped to the API. DNS: wildcard *.run.cns.me -> ghs.googlehosted.com (no per-service record needed). Mapping created via gcloud — see DEPLOYMENT.md."
  default     = "tenders.run.cns.me"
}

variable "bq_location" {
  type    = string
  default = "EU"
}

variable "raw_dataset" {
  type    = string
  default = "uk_tenders_raw"
}

variable "public_dataset" {
  type    = string
  default = "uk_tenders_public"
}

variable "api_image" {
  type        = string
  description = "Fully-qualified API container image (Artifact Registry)."
  default     = ""
}

variable "ingest_image" {
  type        = string
  description = "Fully-qualified ingestion container image."
  default     = ""
}

variable "max_bytes_billed" {
  type    = string
  default = "2147483648" # 2 GiB
}
