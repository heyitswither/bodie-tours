#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="bodie-tours-prod"
REGION="us-west2"
RUNTIME="python314"
LOG_FILE="deploy.log"

# Parse targets to allow selection of endpoints for deployment, default to all
TARGETS=("$@")

should_deploy() {
  local func_name="$1"
  if [ ${#TARGETS[@]} -eq 0 ]; then
    return 0
  fi
  for target in "${TARGETS[@]}"; do
    if [ "$target" = "$func_name" ]; then
      return 0
    fi
  done
  return 1
}

# Resolve default Compute service account if SERVICE_ACCOUNT_EMAIL is not set in environment
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
DEFAULT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_EMAIL:-$DEFAULT_SA}"

# Clear previous deploy log
> "$LOG_FILE"

echo "=== Deploying Bodie Tours Cloud Functions ==="
echo "Logs are being redirected to $LOG_FILE"

# 1. handle-booking
if should_deploy "handle-booking"; then
  echo "Deploying handle-booking..."
  gcloud functions deploy handle-booking \
    --gen2 \
    --runtime="$RUNTIME" \
    --region="$REGION" \
    --trigger-http \
    --allow-unauthenticated \
    --entry-point=handle_booking \
    --project="$PROJECT_ID" \
    --set-env-vars LOG_EXECUTION_ID="true",TOUR_PRICE_PER_PERSON="25.00",EMAIL_TEMPLATE_TYPE="custom" >> "$LOG_FILE" 2>&1
fi

# 2. qbo-login
if should_deploy "qbo-login"; then
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
fi

# 3. qbo-callback
if should_deploy "qbo-callback"; then
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
fi

# 4. m365-login
if should_deploy "m365-login"; then
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
fi

# 5. m365-callback
if should_deploy "m365-callback"; then
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
fi

# 6. qbo-webhook
if should_deploy "qbo-webhook"; then
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
fi

# 7. prune-unpaid-slots (secured) with OIDC audience
if should_deploy "prune-unpaid-slots"; then
  echo "Deploying prune-unpaid-slots..."
  gcloud functions deploy prune-unpaid-slots \
    --gen2 \
    --runtime="$RUNTIME" \
    --region="$REGION" \
    --trigger-http \
    --no-allow-unauthenticated \
    --entry-point=prune_unpaid_slots \
    --project="$PROJECT_ID" \
    --set-env-vars LOG_EXECUTION_ID="true",EMAIL_TEMPLATE_TYPE="custom" >> "$LOG_FILE" 2>&1

  # Fetch the generated URI
  PRUNE_URL=$(gcloud functions describe prune-unpaid-slots --gen2 --region="$REGION" --project="$PROJECT_ID" --format="value(serviceConfig.uri)")

  # Update the function with the explicit OIDC audience variable
  gcloud functions deploy prune-unpaid-slots \
    --gen2 \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --update-env-vars OIDC_AUDIENCE="$PRUNE_URL" >> "$LOG_FILE" 2>&1
fi

# 9. retry-unpaid-bookings (secured) with OIDC audience
if should_deploy "retry-unpaid-bookings"; then
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

  # Create or update Cloud Scheduler job to run retry-unpaid-bookings every 15 minutes
  if gcloud scheduler jobs describe retry-unpaid-bookings-job --location="$REGION" --project="$PROJECT_ID" > /dev/null 2>&1; then
    gcloud scheduler jobs update http retry-unpaid-bookings-job \
      --schedule "*/15 * * * *" \
      --uri "$RETRY_URL" \
      --http-method POST \
      --oidc-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
      --oidc-token-audience "$RETRY_URL" \
      --location "$REGION" \
      --project="$PROJECT_ID" >> "$LOG_FILE" 2>&1
  else
    gcloud scheduler jobs create http retry-unpaid-bookings-job \
      --schedule "*/15 * * * *" \
      --uri "$RETRY_URL" \
      --http-method POST \
      --oidc-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
      --oidc-token-audience "$RETRY_URL" \
      --location "$REGION" \
      --project="$PROJECT_ID" >> "$LOG_FILE" 2>&1
  fi
fi

# 8. m365-free-availability
if should_deploy "m365-free-availability"; then
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
fi

echo "=== Selected Cloud Functions Deployed Successfully ==="
