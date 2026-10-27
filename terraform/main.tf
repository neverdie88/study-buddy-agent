# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  services = [
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "discoveryengine.googleapis.com",
    "calendar-json.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- storage
resource "google_firestore_database" "buddy" {
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "buddy" {
  location      = var.region
  repository_id = "study-buddy"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# ---------------------------------------------------------------- secrets (values added out-of-band)
resource "google_secret_manager_secret" "calendar_credentials" {
  secret_id = "study-buddy-calendar-credentials"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- school materials search (optional)
resource "google_discovery_engine_data_store" "school_materials" {
  count             = var.create_vertex_search ? 1 : 0
  location          = "global"
  data_store_id     = "study-buddy-school-materials"
  display_name      = "Study Buddy School Materials"
  industry_vertical = "GENERIC"
  content_config    = "CONTENT_REQUIRED"
  solution_types    = ["SOLUTION_TYPE_SEARCH"]
  depends_on        = [google_project_service.apis]
}

# ---------------------------------------------------------------- Cloud Run
resource "google_cloud_run_v2_service" "buddy" {
  name                = "study-buddy-agent"
  location            = var.region
  deletion_protection = false
  invoker_iam_disabled = var.public_access

  template {
    service_account = google_service_account.buddy.email
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "BUDDY_STORAGE_BACKEND"
        value = "firestore"
      }
      env {
        name  = "BUDDY_ENABLE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name  = "BUDDY_ENABLE_CLOUD_DLP"
        value = "true"
      }
      env {
        name  = "BUDDY_SESSION_DB_URL"
        value = "sqlite+aiosqlite:////tmp/adk_sessions.db"
      }
      env {
        name  = "BUDDY_SQLITE_PATH"
        value = "/tmp/study_buddy.db"
      }
      env {
        name  = "BUDDY_VERTEX_SEARCH_DATASTORE"
        value = var.create_vertex_search ? google_discovery_engine_data_store.school_materials[0].name : ""
      }
    }
  }
  depends_on = [google_project_service.apis, google_project_iam_member.buddy_roles]
}
