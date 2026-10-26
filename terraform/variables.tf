variable "project_id" {
  description = "Google Cloud project id."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run and Artifact Registry."
  type        = string
  default     = "us-west1"
}

variable "firestore_location" {
  description = "Firestore location."
  type        = string
  default     = "us-west1"
}

variable "image" {
  description = "Container image for the Study Buddy service."
  type        = string
}

variable "public_access" {
  description = "Disable the Cloud Run invoker IAM check (use IAP / auth in production)."
  type        = bool
  default     = true
}

variable "create_vertex_search" {
  description = "Create a Vertex AI Search data store for uploaded school materials."
  type        = bool
  default     = false
}
