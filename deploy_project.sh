#!/usr/bin/env bash
#
# Bodie State Park Tours - End-to-End Project Deployment Script
# This script automates prerequisites validation, Firestore configuration/template seeding,
# and deploys Google Cloud Functions using deploy_functions.sh.
#
# Usage: ./deploy_project.sh [options] [function-targets...]
#

set -euo pipefail

# ANSI color codes for premium visual formatting
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0;0m' # No Color

# Default values
DEFAULT_PROJECT_ID="bodie-tours-prod"
PROJECT_ID=""
SKIP_SEEDING=false
SKIP_TOKEN_CHECK=false
FORWARD_ARGS=()

# Show usage instructions
show_usage() {
  echo -e "${BOLD}Bodie State Park Tours End-to-End Deployment Script${NC}"
  echo -e "Usage: $0 [options] [function-targets...]"
  echo ""
  echo -e "${BOLD}Options:${NC}"
  echo -e "  -h, --help            Show this help message and exit"
  echo -e "  -p, --project ID      Override the GCP Project ID (default: read from deploy_functions.sh or use bodie-tours-prod)"
  echo -e "  -s, --skip-seeding    Skip Firestore configuration and email template seeding"
  echo -e "  -t, --skip-token-check Skip QuickBooks & M365 OAuth token check and refresh"
  echo ""
  echo -e "${BOLD}Function Targets:${NC}"
  echo -e "  Pass one or more specific Cloud Function names to deploy only those (default: deploy all):"
  echo -e "  - ${CYAN}handle-booking${NC}"
  echo -e "  - ${CYAN}qbo-login${NC}"
  echo -e "  - ${CYAN}qbo-callback${NC}"
  echo -e "  - ${CYAN}m365-login${NC}"
  echo -e "  - ${CYAN}m365-callback${NC}"
  echo -e "  - ${CYAN}qbo-webhook${NC}"
  echo -e "  - ${CYAN}prune-unpaid-slots${NC}"
  echo -e "  - ${CYAN}retry-unpaid-bookings${NC}"
  echo -e "  - ${CYAN}m365-free-availability${NC}"
  echo ""
  echo -e "Example: $0 --project my-custom-gcp-project handle-booking prune-unpaid-slots"
}

# Parse command line options
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      show_usage
      exit 0
      ;;
    -p|--project)
      if [[ -z "${2:-}" ]]; then
        echo -e "${RED}Error: --project requires an argument.${NC}" >&2
        exit 1
      fi
      PROJECT_ID="$2"
      shift 2
      ;;
    -s|--skip-seeding)
      SKIP_SEEDING=true
      shift
      ;;
    -t|--skip-token-check)
      SKIP_TOKEN_CHECK=true
      shift
      ;;
    -*)
      echo -e "${RED}Error: Unknown option $1${NC}" >&2
      show_usage
      exit 1
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

echo -e "${BLUE}===================================================================${NC}"
echo -e "${BOLD}${GREEN}        BODIE STATE PARK TOURS - END-TO-END DEPLOYMENT${NC}"
echo -e "${BLUE}===================================================================${NC}"

# 1. Resolve GCP Project ID
if [[ -z "$PROJECT_ID" ]]; then
  # Try to extract PROJECT_ID from deploy_functions.sh
  if [[ -f "deploy_functions.sh" ]]; then
    EXTRACTED_PROJECT=$(grep -E '^PROJECT_ID=' deploy_functions.sh | head -n 1 | cut -d'"' -f2 || echo "")
    if [[ -n "$EXTRACTED_PROJECT" ]]; then
      PROJECT_ID="$EXTRACTED_PROJECT"
      echo -e "${GREEN}✔ Resolved Project ID from deploy_functions.sh:${NC} ${BOLD}$PROJECT_ID${NC}"
    fi
  fi
fi

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="$DEFAULT_PROJECT_ID"
  echo -e "${YELLOW}⚠ Could not resolve Project ID from script. Falling back to default:${NC} ${BOLD}$PROJECT_ID${NC}"
fi

# 2. Check Prerequisites
echo -e "\n${BOLD}${CYAN}Step 1: Validating Prerequisites...${NC}"

# Check gcloud CLI
if ! command -v gcloud &>/dev/null; then
  echo -e "${RED}✘ Error: gcloud CLI is not installed.${NC}" >&2
  echo -e "Please install the Google Cloud SDK first: https://cloud.google.com/sdk" >&2
  exit 1
fi
echo -e "${GREEN}✔ gcloud CLI is installed.${NC}"

# Verify active project in gcloud configuration
ACTIVE_GCLOUD_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "")
if [[ -n "$ACTIVE_GCLOUD_PROJECT" ]]; then
  echo -e "${GREEN}✔ Active gcloud project configuration:${NC} ${BOLD}$ACTIVE_GCLOUD_PROJECT${NC}"
  if [[ "$ACTIVE_GCLOUD_PROJECT" != "$PROJECT_ID" ]]; then
    echo -e "${YELLOW}⚠ Warning: Active gcloud project ($ACTIVE_GCLOUD_PROJECT) differs from deploy target ($PROJECT_ID).${NC}"
    echo -e "We will pass --project=$PROJECT_ID to gcloud commands."
  fi
else
  echo -e "${YELLOW}⚠ No active gcloud project configured.${NC}"
fi

# 3. Locate correct Python Environment for Seeding
echo -e "\n${BOLD}${CYAN}Step 2: Resolving Python Seeding Environment...${NC}"
PYTHON_EXEC=""

for env in .venv venv myenv; do
  if [[ -f "$env/bin/python" ]]; then
    if "$env/bin/python" -c "import google.cloud.firestore" &>/dev/null; then
      PYTHON_EXEC="$env/bin/python"
      echo -e "${GREEN}✔ Found Python environment with Firestore support:${NC} ${BOLD}$env/bin/python${NC}"
      break
    fi
  fi
done

if [[ -z "$PYTHON_EXEC" ]]; then
  # Try system python3
  if command -v python3 &>/dev/null && python3 -c "import google.cloud.firestore" &>/dev/null; then
    PYTHON_EXEC="python3"
    echo -e "${GREEN}✔ Found system python3 with Firestore support.${NC}"
  elif command -v python &>/dev/null && python -c "import google.cloud.firestore" &>/dev/null; then
    PYTHON_EXEC="python"
    echo -e "${GREEN}✔ Found system python with Firestore support.${NC}"
  fi
fi

if [[ -z "$PYTHON_EXEC" ]]; then
  echo -e "${RED}✘ Error: Could not find a Python environment with 'google-cloud-firestore' installed.${NC}" >&2
  echo -e "Please activate your virtual environment or install dependencies first:" >&2
  echo -e "  ${BOLD}pip install -r requirements.txt${NC}" >&2
  exit 1
fi

# 4. Perform Firestore Database and Template Seeding
if [[ "$SKIP_SEEDING" = "true" ]]; then
  echo -e "\n${YELLOW}➡ Skipping Firestore seeding step as requested.${NC}"
else
  echo -e "\n${BOLD}${CYAN}Step 3: Seeding Firestore Database & Configurations...${NC}"
  
  # Ensure named database environment variable is respected (if needed by Google SDK)
  export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
  
  # Seed Email Templates
  echo -e "${BLUE}Running template seeding (seed_templates.py)...${NC}"
  if "$PYTHON_EXEC" seed_templates.py; then
    echo -e "${GREEN}✔ Email templates successfully seeded to Firestore!${NC}"
  else
    echo -e "${RED}✘ Error: Failed to seed email templates.${NC}" >&2
    echo -e "Ensure your Google Cloud credentials/project context allows access to Firestore database 'bodie-tours'.${NC}" >&2
    exit 1
  fi
  
  # Seed Tour Configurations
  echo -e "${BLUE}Running tour configuration seeding (tours_config.py)...${NC}"
  if "$PYTHON_EXEC" tours_config.py; then
    echo -e "${GREEN}✔ Tour configurations successfully seeded to Firestore config/tours!${NC}"
  else
    echo -e "${RED}✘ Error: Failed to seed tour configurations.${NC}" >&2
    exit 1
  fi
fi

# 5. Invoke deploy_functions.sh for Cloud Functions Deployments
echo -e "\n${BOLD}${CYAN}Step 4: Deploying Google Cloud Functions...${NC}"

if [[ ! -f "deploy_functions.sh" ]]; then
  echo -e "${RED}✘ Error: deploy_functions.sh not found in the current directory.${NC}" >&2
  exit 1
fi

chmod +x deploy_functions.sh

# Let's override PROJECT_ID in the environment for deploy_functions.sh if specified
export PROJECT_ID="$PROJECT_ID"

if [[ ${#FORWARD_ARGS[@]} -gt 0 ]]; then
  echo -e "${BLUE}Invoking deploy_functions.sh with specific targets: ${BOLD}${FORWARD_ARGS[*]}${NC}"
  ./deploy_functions.sh "${FORWARD_ARGS[@]}"
else
  echo -e "${BLUE}Invoking deploy_functions.sh to deploy all Cloud Functions...${NC}"
  ./deploy_functions.sh
fi

# 6. Check and Refresh OAuth Tokens / Perform Login Flow
if [[ "$SKIP_TOKEN_CHECK" = "true" ]]; then
  echo -e "\n${YELLOW}➡ Skipping Integration OAuth token check and refresh as requested.${NC}"
else
  echo -e "\n${BOLD}${CYAN}Step 5: Verifying & Refreshing Integration OAuth Tokens...${NC}"
  export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
  if "$PYTHON_EXEC" verify_integrations.py --tokens-only; then
    echo -e "${GREEN}✔ Integration OAuth tokens successfully verified and refreshed!${NC}"
  else
    echo -e "${RED}✘ Error: Integration OAuth token verification or refresh failed.${NC}" >&2
    echo -e "If initial authorization is required, please complete the login flow via the launched browser.${NC}" >&2
    exit 1
  fi
fi

echo -e "\n${BLUE}===================================================================${NC}"
echo -e "${BOLD}${GREEN}        END-TO-END DEPLOYMENT COMPLETED SUCCESSFULLY!${NC}"
echo -e "${BLUE}===================================================================${NC}"
echo -e "Your Bodie State Park booking system has been fully configured and updated:"
echo -e " 1. ${GREEN}Firestore Seeding:${NC} Email templates & 5 official Tour configurations uploaded."
echo -e " 2. ${GREEN}Cloud Functions:${NC} Functions deployed successfully to GCP project '${BOLD}$PROJECT_ID${NC}'."
echo -e " 3. ${GREEN}Integration OAuth Tokens:${NC} Tokens verified, refreshed, and authorized in Firestore."
echo -e " 4. ${GREEN}Monitoring:${NC} Logs available in ${BOLD}deploy.log${NC}."
echo ""
echo -e "For subsequent manual verification or local testing, you can execute:"
echo -e "  - ${CYAN}./run_verification.sh${NC} (runs Puppeteer automated visual audits and saves screenshots)"
echo -e "==================================================================="
