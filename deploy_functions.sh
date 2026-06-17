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
echo "Deploying in parallel. Logs are buffered into temporary files and consolidated into $LOG_FILE."

pids=()
funcs=()

# 1. handle-booking
if should_deploy "handle-booking"; then
  (
    echo "=== Starting deployment of handle-booking ==="
    gcloud functions deploy handle-booking \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=handle_booking \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true",TOUR_PRICE_PER_PERSON="1.00",EMAIL_TEMPLATE_TYPE="custom"
  ) > "deploy_handle-booking.log" 2>&1 &
  pids+=($!)
  funcs+=("handle-booking")
fi

# 2. qbo-login
if should_deploy "qbo-login"; then
  (
    echo "=== Starting deployment of qbo-login ==="
    gcloud functions deploy qbo-login \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=qbo_login \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true"
  ) > "deploy_qbo-login.log" 2>&1 &
  pids+=($!)
  funcs+=("qbo-login")
fi

# 3. qbo-callback
if should_deploy "qbo-callback"; then
  (
    echo "=== Starting deployment of qbo-callback ==="
    gcloud functions deploy qbo-callback \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=qbo_callback \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true"
  ) > "deploy_qbo-callback.log" 2>&1 &
  pids+=($!)
  funcs+=("qbo-callback")
fi

# 4. m365-login
if should_deploy "m365-login"; then
  (
    echo "=== Starting deployment of m365-login ==="
    gcloud functions deploy m365-login \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=m365_login \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true"
  ) > "deploy_m365-login.log" 2>&1 &
  pids+=($!)
  funcs+=("m365-login")
fi

# 5. m365-callback
if should_deploy "m365-callback"; then
  (
    echo "=== Starting deployment of m365-callback ==="
    gcloud functions deploy m365-callback \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=m365_callback \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true"
  ) > "deploy_m365-callback.log" 2>&1 &
  pids+=($!)
  funcs+=("m365-callback")
fi

# 6. qbo-webhook
if should_deploy "qbo-webhook"; then
  (
    echo "=== Starting deployment of qbo-webhook ==="
    gcloud functions deploy qbo-webhook \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=qbo_webhook \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true"
  ) > "deploy_qbo-webhook.log" 2>&1 &
  pids+=($!)
  funcs+=("qbo-webhook")
fi

# 7. prune-unpaid-slots (secured)
if should_deploy "prune-unpaid-slots"; then
  (
    echo "=== Starting deployment of prune-unpaid-slots ==="
    gcloud functions deploy prune-unpaid-slots \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --no-allow-unauthenticated \
      --entry-point=prune_unpaid_slots \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true",EMAIL_TEMPLATE_TYPE="custom"

    echo "Fetching generated URL for prune-unpaid-slots..."
    PRUNE_URL=$(gcloud functions describe prune-unpaid-slots --gen2 --region="$REGION" --project="$PROJECT_ID" --format="value(serviceConfig.uri)")

    echo "Creating or updating Cloud Scheduler job for pruning..."
    if gcloud scheduler jobs describe prune-unpaid-slots-job --location="$REGION" --project="$PROJECT_ID" > /dev/null 2>&1; then
      gcloud scheduler jobs update http prune-unpaid-slots-job \
        --schedule "*/15 * * * *" \
        --uri "$PRUNE_URL" \
        --http-method POST \
        --oidc-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
        --oidc-token-audience "$PRUNE_URL" \
        --location "$REGION" \
        --project="$PROJECT_ID"
    else
      gcloud scheduler jobs create http prune-unpaid-slots-job \
        --schedule "*/15 * * * *" \
        --uri "$PRUNE_URL" \
        --http-method POST \
        --oidc-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
        --oidc-token-audience "$PRUNE_URL" \
        --location "$REGION" \
        --project="$PROJECT_ID"
    fi
  ) > "deploy_prune-unpaid-slots.log" 2>&1 &
  pids+=($!)
  funcs+=("prune-unpaid-slots")
fi

# 9. retry-unpaid-bookings (secured)
if should_deploy "retry-unpaid-bookings"; then
  (
    echo "=== Starting deployment of retry-unpaid-bookings ==="
    gcloud functions deploy retry-unpaid-bookings \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --no-allow-unauthenticated \
      --entry-point=retry_unpaid_bookings \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true",MAX_RETRY_ATTEMPTS="10"

    echo "Fetching generated URL for retry-unpaid-bookings..."
    RETRY_URL=$(gcloud functions describe retry-unpaid-bookings --gen2 --region="$REGION" --project="$PROJECT_ID" --format="value(serviceConfig.uri)")

    echo "Creating or updating Cloud Scheduler job..."
    if gcloud scheduler jobs describe retry-unpaid-bookings-job --location="$REGION" --project="$PROJECT_ID" > /dev/null 2>&1; then
      gcloud scheduler jobs update http retry-unpaid-bookings-job \
        --schedule "*/15 * * * *" \
        --uri "$RETRY_URL" \
        --http-method POST \
        --oidc-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
        --oidc-token-audience "$RETRY_URL" \
        --location "$REGION" \
        --project="$PROJECT_ID"
    else
      gcloud scheduler jobs create http retry-unpaid-bookings-job \
        --schedule "*/15 * * * *" \
        --uri "$RETRY_URL" \
        --http-method POST \
        --oidc-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
        --oidc-token-audience "$RETRY_URL" \
        --location "$REGION" \
        --project="$PROJECT_ID"
    fi
  ) > "deploy_retry-unpaid-bookings.log" 2>&1 &
  pids+=($!)
  funcs+=("retry-unpaid-bookings")
fi

# 8. m365-free-availability
if should_deploy "m365-free-availability"; then
  (
    echo "=== Starting deployment of m365-free-availability ==="
    gcloud functions deploy m365-free-availability \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=m365_free_availability \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true"
  ) > "deploy_m365-free-availability.log" 2>&1 &
  pids+=($!)
  funcs+=("m365-free-availability")
fi

# 10. cancel-tour
if should_deploy "cancel-tour"; then
  (
    echo "=== Starting deployment of cancel-tour ==="
    gcloud functions deploy cancel-tour \
      --gen2 \
      --source=backend \
      --runtime="$RUNTIME" \
      --region="$REGION" \
      --trigger-http \
      --allow-unauthenticated \
      --entry-point=cancel_tour \
      --project="$PROJECT_ID" \
      --set-env-vars LOG_EXECUTION_ID="true"
  ) > "deploy_cancel-tour.log" 2>&1 &
  pids+=($!)
  funcs+=("cancel-tour")
fi

# Wait for all deployments and handle outputs
errors=0
for i in "${!pids[@]}"; do
  pid="${pids[$i]}"
  func="${funcs[$i]}"
  log_file="deploy_${func}.log"

  echo "Waiting for $func to finish..."

  if wait "$pid"; then
    echo "✓ $func deployed successfully!"
    echo "----------------------------" >> "$LOG_FILE"
    echo "Successful deployment: $func" >> "$LOG_FILE"
    echo "----------------------------" >> "$LOG_FILE"
    cat "$log_file" >> "$LOG_FILE"
    rm -f "$log_file"
  else
    echo "✗ $func deployment FAILED!"
    echo "----------------------------" >> "$LOG_FILE"
    echo "FAILED deployment: $func" >> "$LOG_FILE"
    echo "----------------------------" >> "$LOG_FILE"
    cat "$log_file" >> "$LOG_FILE"
    # Print error output directly to console for quick visibility
    echo "=== Error Log for $func ==="
    cat "$log_file"
    echo "=============================="
    rm -f "$log_file"
    errors=$((errors + 1))
  fi
done

if [ "$errors" -ne 0 ]; then
  echo "=== Deployment failed for $errors function(s). ==="
  echo "Check $LOG_FILE for full logs."
  exit 1
else
  echo "=== Selected Cloud Functions Deployed Successfully ==="
  echo "All deployment outputs consolidated in $LOG_FILE."
fi
