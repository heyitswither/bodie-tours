#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="bodie-tours-prod"
REGION="us-west2"
RUNTIME="python310"
LOG_FILE="deploy.log"

# Resolve default Compute service account if SERVICE_ACCOUNT_EMAIL is not set in environment
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
DEFAULT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_EMAIL:-$DEFAULT_SA}"

# Clear previous deploy log
> "$LOG_FILE"

echo "=== Deploying Bodie Tours Cloud Functions ==="
echo "Logs are being redirected to $LOG_FILE"

# 1. handle-booking
echo "Deploying handle-booking..."
gcloud functions deploy handle-booking \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point=handle_booking \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true",QBO_ENVIRONMENT="sandbox",TOUR_PRICE_PER_PERSON="25.00" >> "$LOG_FILE" 2>&1

# 2. qbo-login
echo "Deploying qbo-login..."
gcloud functions deploy qbo-login \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point=qbo_login \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true" >> "$LOG_FILE" 2>&1

# 3. qbo-callback
echo "Deploying qbo-callback..."
gcloud functions deploy qbo-callback \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point=qbo_callback \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true" >> "$LOG_FILE" 2>&1

# 4. m365-login
echo "Deploying m365-login..."
gcloud functions deploy m365-login \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point=m365_login \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true" >> "$LOG_FILE" 2>&1

# 5. m365-callback
echo "Deploying m365-callback..."
gcloud functions deploy m365-callback \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point=m365_callback \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true" >> "$LOG_FILE" 2>&1

# 6. qbo-webhook
echo "Deploying qbo-webhook..."
gcloud functions deploy qbo-webhook \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point=qbo_webhook \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true" >> "$LOG_FILE" 2>&1

# 7. prune-unpaid-slots (secured) with OIDC audience

echo "Deploying prune-unpaid-slots..."

gcloud functions deploy prune-unpaid-slots \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --no-allow-unauthenticated \
  --entry-point=prune_unpaid_slots \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true" >> "$LOG_FILE" 2>&1

# Fetch the generated URI
PRUNE_URL=$(gcloud functions describe prune-unpaid-slots --gen2 --region="$REGION" --project="$PROJECT_ID" --format="value(serviceConfig.uri)")

# Update the function with the explicit OIDC audience variable
gcloud functions deploy prune-unpaid-slots \
  --gen2 \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --update-env-vars OIDC_AUDIENCE="$PRUNE_URL" >> "$LOG_FILE" 2>&1

# 9. retry-unpaid-bookings (secured) with OIDC audience

echo "Deploying retry-unpaid-bookings..."

gcloud functions deploy retry-unpaid-bookings \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --no-allow-unauthenticated \
  --entry-point=retry_unpaid_bookings \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true",MAX_RETRY_ATTEMPTS="10" >> "$LOG_FILE" 2>&1

# Fetch the generated URI for retry function
RETRY_URL=$(gcloud functions describe retry-unpaid-bookings --gen2 --region="$REGION" --project="$PROJECT_ID" --format="value(serviceConfig.uri)")

# Update the function with the explicit OIDC audience variable
gcloud functions deploy retry-unpaid-bookings \
  --gen2 \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --update-env-vars OIDC_AUDIENCE="$RETRY_URL" >>"$LOG_FILE" 2>&1

# Create Cloud Scheduler job to run retry-unpaid-bookings every 15 minutes

gcloud scheduler jobs create http retry-unpaid-bookings-job \
  --schedule "*/15 * * * *" \
  --uri "$RETRY_URL" \
  --http-method POST \
  --oidc-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
  --oidc-token-audience "$RETRY_URL" \
  --location "$REGION" \
  --project="$PROJECT_ID" >> "$LOG_FILE" 2>&1

# 8. m365-free-availability
echo "Deploying m365-free-availability..."
gcloud functions deploy m365-free-availability \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point=m365_free_availability \
  --project="$PROJECT_ID" \
  --set-env-vars LOG_EXECUTION_ID="true" >> "$LOG_FILE" 2>&1

echo "=== All Cloud Functions Deployed Successfully ==="
