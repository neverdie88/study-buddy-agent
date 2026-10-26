output "service_url" {
  description = "Public URL of Pip the Study Buddy."
  value       = google_cloud_run_v2_service.buddy.uri
}

output "runtime_service_account" {
  value = google_service_account.buddy.email
}

output "artifact_registry_repo" {
  value = google_artifact_registry_repository.buddy.id
}
