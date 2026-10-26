#!/usr/bin/env bash
# Reproducible deployment for Pip the Study Buddy.
#
#   ./scripts/deploy.sh cloud-run      # build + deploy the web app (voice + whiteboard UI) to Cloud Run
#   ./scripts/deploy.sh agent-runtime  # deploy the ADK agent to Vertex AI Agent Runtime via agents-cli
#   ./scripts/deploy.sh terraform      # provision all infrastructure with Terraform
#
# Requires: gcloud (authenticated), and optionally agents-cli / terraform.
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${REGION:-us-west1}"
SERVICE="${SERVICE:-study-buddy-agent}"
RUNTIME_SA="study-buddy-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

cmd="${1:-cloud-run}"

ensure_runtime_sa() {
  if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create study-buddy-runtime --display-name "Pip the Study Buddy runtime" --project "$PROJECT_ID"
  fi
  for role in roles/aiplatform.user roles/datastore.user roles/secretmanager.secretAccessor roles/dlp.user \
              roles/cloudtrace.agent roles/logging.logWriter; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${RUNTIME_SA}" \
      --role "$role" --condition=None --quiet >/dev/null
  done
}

case "$cmd" in
  cloud-run)
    gcloud services enable run.googleapis.com cloudbuild.googleapis.com aiplatform.googleapis.com \
      firestore.googleapis.com secretmanager.googleapis.com dlp.googleapis.com cloudtrace.googleapis.com \
      --project "$PROJECT_ID"
    gcloud firestore databases describe --project "$PROJECT_ID" >/dev/null 2>&1 || \
      gcloud firestore databases create --location "$REGION" --project "$PROJECT_ID"
    ensure_runtime_sa
    gcloud run deploy "$SERVICE" \
      --source . \
      --project "$PROJECT_ID" \
      --region "$REGION" \
      --service-account "$RUNTIME_SA" \
      --memory 1Gi --cpu 1 --min-instances 0 --max-instances 2 \
      --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true,BUDDY_STORAGE_BACKEND=firestore,BUDDY_ENABLE_CLOUD_TRACE=true,BUDDY_ENABLE_CLOUD_DLP=true,BUDDY_SQLITE_PATH=/tmp/study_buddy.db,BUDDY_SESSION_DB_URL=sqlite+aiosqlite:////tmp/adk_sessions.db" \
      --no-allow-unauthenticated \
      --quiet
    # Public demo access without an allUsers IAM binding (works with Domain Restricted Sharing).
    gcloud run services update "$SERVICE" --region "$REGION" --project "$PROJECT_ID" --no-invoker-iam-check --quiet
    gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)'
    ;;
  agent-runtime)
    # Deploys the ADK `app` (app/agent.py) as a managed Agent Runtime with a managed session service.
    agents-cli deploy --project "$PROJECT_ID" --region "$REGION"
    ;;
  terraform)
    IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/study-buddy/${SERVICE}:$(git rev-parse --short HEAD)"
    gcloud builds submit --tag "$IMAGE" --project "$PROJECT_ID"
    terraform -chdir=terraform init
    terraform -chdir=terraform apply -var "project_id=${PROJECT_ID}" -var "region=${REGION}" -var "image=${IMAGE}"
    ;;
  *)
    echo "Unknown command: $cmd" >&2; exit 1;;
esac
