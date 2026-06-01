# UK Tenders MCP — infrastructure (ADR-0001/0003/0005).
# Datasets + IAM + GCS + Cloud Run service (API) + Cloud Run Job (ingestion) + nightly Scheduler.
# Tables are created by the ingestion job's `--bootstrap` step from sql/schema.sql
# (single source of truth), so they are not duplicated as HCL here.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = ">= 5.0" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

# --- datasets: least-privilege read boundary (raw=write, public=read) ----------
resource "google_bigquery_dataset" "raw" {
  dataset_id  = var.raw_dataset
  location    = var.bq_location
  description = "UK Tenders MCP — raw OCDS event log (PII-bearing, write-only by ingestion)"
}

resource "google_bigquery_dataset" "public" {
  dataset_id  = var.public_dataset
  location    = var.bq_location
  description = "UK Tenders MCP — query-serving, read-only by API"
}

# --- service accounts ----------------------------------------------------------
resource "google_service_account" "ingest" {
  account_id   = "uk-tenders-ingest"
  display_name = "UK Tenders MCP ingestion (write)"
}

resource "google_service_account" "api" {
  account_id   = "uk-tenders-api"
  display_name = "UK Tenders MCP API (read-only)"
}

# --- IAM: ingestion writes both datasets; API reads ONLY the public dataset ----
resource "google_bigquery_dataset_iam_member" "ingest_raw" {
  dataset_id = google_bigquery_dataset.raw.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_bigquery_dataset_iam_member" "ingest_public" {
  dataset_id = google_bigquery_dataset.public.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_bigquery_dataset_iam_member" "api_public_read" {
  dataset_id = google_bigquery_dataset.public.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "ingest_jobuser" {
  project = var.project
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_project_iam_member" "api_jobuser" {
  project = var.project
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# --- GCS raw archive (replay source; access-controlled write tier) -------------
resource "google_storage_bucket" "raw_archive" {
  name                        = "${var.project}-uk-tenders-raw"
  location                    = var.bq_location
  uniform_bucket_level_access = true
  lifecycle_rule {
    condition { age = 730 }
    action { type = "Delete" }
  }
}

# --- API: public, unauthenticated MCP, read-only SA, single instance ----------
resource "google_cloud_run_v2_service" "api" {
  count    = var.api_image == "" ? 0 : 1
  name     = "uk-tenders-mcp"
  location = var.region
  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    session_affinity = true
    containers {
      image = var.api_image
      ports { container_port = 8080 }
      env {
        name  = "GCP_PROJECT"
        value = var.project
      }
      env {
        name  = "BQ_PUBLIC_DATASET"
        value = var.public_dataset
      }
      env {
        name  = "BQ_LOCATION"
        value = var.bq_location
      }
      env {
        name  = "MAX_BYTES_BILLED"
        value = var.max_bytes_billed
      }
      resources { limits = { cpu = "1", memory = "512Mi" } }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "api_public" {
  count    = var.api_image == "" ? 0 : 1
  name     = google_cloud_run_v2_service.api[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- ingestion job + nightly schedule ------------------------------------------
resource "google_cloud_run_v2_job" "ingest" {
  count    = var.ingest_image == "" ? 0 : 1
  name     = "uk-tenders-ingest"
  location = var.region
  template {
    template {
      service_account = google_service_account.ingest.email
      timeout         = "3600s"
      max_retries     = 1
      containers {
        image = var.ingest_image
        args  = ["--source", "fts", "--mode", "nightly"]
        env {
          name  = "GCP_PROJECT"
          value = var.project
        }
        env {
          name  = "BQ_LOCATION"
          value = var.bq_location
        }
        env {
          name  = "BQ_RAW_DATASET"
          value = var.raw_dataset
        }
        env {
          name  = "BQ_PUBLIC_DATASET"
          value = var.public_dataset
        }
        resources { limits = { cpu = "1", memory = "1Gi" } }
      }
    }
  }
}

resource "google_service_account" "scheduler" {
  account_id   = "uk-tenders-scheduler"
  display_name = "UK Tenders MCP nightly scheduler"
}

resource "google_project_iam_member" "scheduler_run" {
  project = var.project
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "nightly" {
  count     = var.ingest_image == "" ? 0 : 1
  name      = "uk-tenders-nightly"
  schedule  = "30 2 * * *"
  time_zone = "Europe/London"
  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project}/jobs/uk-tenders-ingest:run"
    oauth_token { service_account_email = google_service_account.scheduler.email }
  }
}
