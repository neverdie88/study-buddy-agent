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
