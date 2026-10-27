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

# Least-privilege runtime identity for the Study Buddy service.
resource "google_service_account" "buddy" {
  account_id   = "study-buddy-runtime"
  display_name = "Pip the Study Buddy runtime"
}

locals {
  runtime_roles = [
    "roles/aiplatform.user",            # Gemini on Vertex AI
    "roles/datastore.user",             # Firestore learner profile + calendar
    "roles/secretmanager.secretAccessor",
    "roles/dlp.user",                   # PII de-identification
    "roles/cloudtrace.agent",           # OpenTelemetry spans
    "roles/logging.logWriter",          # Structured JSON logs
    "roles/discoveryengine.viewer",     # Vertex AI Search over school materials
  ]
}

resource "google_project_iam_member" "buddy_roles" {
  for_each = toset(local.runtime_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.buddy.email}"
}
