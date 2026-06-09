# Walkthrough Verification & Deployment Guide

This document details the step‑by‑step verification results for the booking widget calendar (`booking_widget.html`) across all five end-user scenarios, followed by an in-depth, non-technical deployment guide for production setup.

## E2E Scenario Verification Results

All five key scenarios have been interactively verified in Chrome via Puppeteer. Visual evidence, console logs, and network traces have been successfully captured and are located in the [screenshots/](file:///home/freya/bodie-tours/screenshots/) directory.

### Scenario 1: Happy Path Successful Booking
- **Status**: Verified Successfully
- **Description**: Select available date -> select open time slot -> fill out contact details -> submit.
- **Verification**: Redirection to QuickBooks Online (QBO) invoice link loaded, M365 event created, and booking ID generated.
- **Screenshot**: ![happy_path](file:///home/freya/bodie-tours/screenshots/happy_path.png)
- **Console Log**: [happy_path_console.log](file:///home/freya/bodie-tours/screenshots/happy_path_console.log)
- **Network Trace**: [happy_path_network.json](file:///home/freya/bodie-tours/screenshots/happy_path_network.json)

### Scenario 2: Sold‑out Slot Handling
- **Status**: Verified Successfully
- **Description**: Click on a date/slot that is marked as SOLD_OUT.
- **Verification**: Clicks are disabled, slot displays "Sold Out", and progression to the next step is blocked.
- **Screenshot**: ![sold_out](file:///home/freya/bodie-tours/screenshots/sold_out.png)
- **Console Log**: [sold_out_console.log](file:///home/freya/bodie-tours/screenshots/sold_out_console.log)
- **Network Trace**: [sold_out_network.json](file:///home/freya/bodie-tours/screenshots/sold_out_network.json)

### Scenario 3: Empty Month with No Availability
- **Status**: Verified Successfully
- **Description**: Navigate to a month with no guide scheduling blocks.
- **Verification**: Widget loads gracefully and renders all days as unavailable without any console errors.
- **Screenshot**: ![empty_month](file:///home/freya/bodie-tours/screenshots/empty_month.png)
- **Console Log**: [empty_month_console.log](file:///home/freya/bodie-tours/screenshots/empty_month_console.log)
- **Network Trace**: [empty_month_network.json](file:///home/freya/bodie-tours/screenshots/empty_month_network.json)

### Scenario 4: Form Validation Failures
- **Status**: Verified Successfully
- **Description**: Submit booking form with invalid email formats or party sizes out of bounds (e.g. 25).
- **Verification**: Submission is blocked, and clear inline validation errors are displayed to the user.
- **Screenshot**: ![validation_failure](file:///home/freya/bodie-tours/screenshots/validation_failure.png)
- **Console Log**: [validation_failure_console.log](file:///home/freya/bodie-tours/screenshots/validation_failure_console.log)
- **Network Trace**: [validation_failure_network.json](file:///home/freya/bodie-tours/screenshots/validation_failure_network.json)

### Scenario 5: Backend Error Simulation
- **Status**: Verified Successfully
- **Description**: Trigger a 409 conflict or 500 error from the backend.
- **Verification**: Frontend catches the API error status and displays a user-friendly error banner: "Booking conflict: The requested time slot is already taken."
- **Screenshot**: ![backend_error](file:///home/freya/bodie-tours/screenshots/backend_error.png)
- **Console Log**: [backend_error_console.log](file:///home/freya/bodie-tours/screenshots/backend_error_console.log)
- **Network Trace**: [backend_error_network.json](file:///home/freya/bodie-tours/screenshots/backend_error_network.json)

---

## Complete Deployment Guide (Non-Technical User Friendly)

This guide provides step-by-step instructions to deploy the entire booking pipeline from scratch. No programming experience is required.

### Step 1: Create a Google Cloud Platform (GCP) Project
1. Open your web browser and go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Sign in with your Google account.
3. Click the project dropdown list at the top of the page (next to "Google Cloud").
4. Click **New Project** in the upper right of the popup window.
5. In the **Project Name** box, type `Bodie State Park Tours`.
6. Leave the Organization and Location defaults as they are and click **Create**.
7. Wait a few seconds for the project to be created. Click **Select Project** in the notifications box.

### Step 2: Set Up Firestore in Native Mode
1. In the search box at the top of the Google Cloud Console, type `Firestore` and click the Firestore result.
2. Click **Create Database**.
3. Set the **Database ID** to `bodie-tours` (do NOT use `(default)`).
4. Select **Native Mode** (do NOT select Datastore Mode). Click **Continue**.
5. In the **Location** dropdown, choose a location closest to you (e.g., `us-west2 (Los Angeles)`).
6. Click **Create Database**.
7. The database is now ready. In the Firestore menu, you will see a list of collections (which will be populated automatically when you run the seeding script and start booking tours).

### Step 3: Set Up a Service Account for Cloud Scheduler
To ensure that only your Cloud Scheduler cron job can trigger the pruning function, we must set up a dedicated Service Account:
1. In the search bar at the top of the console, search for `IAM & Admin` and select **IAM**.
2. In the left-hand navigation menu, click **Service Accounts**.
3. Click **Create Service Account** at the top.
4. Type `cloud-scheduler-invoker` in the Service Account Name field, then click **Create and Continue**.
5. Under **Grant this service account access to project**, click the role dropdown and search for `Cloud Functions Invoker`. Select the role **Cloud Functions Invoker**.
6. Click **Done** to finalize.

### Step 4: Deploy the Cloud Functions
You will deploy two main Cloud Functions: `handle-booking` and `prune-unpaid-slots`.

#### A. Deploy `handle-booking` (Public Endpoint)
This function handles public bookings, QuickBooks invoice generation, and Microsoft 365 events.
1. Open your terminal (command line) and navigate to the project directory containing `main.py`.
2. Run the following command (substitute with your actual values):
   ```bash
   gcloud functions deploy handle-booking \
     --runtime python310 \
     --trigger-http \
     --allow-unauthenticated \
     --entry-point handle_booking \
     --region us-west2 \
     --set-env-vars QBO_CLIENT_ID="YOUR_QBO_CLIENT_ID",QBO_CLIENT_SECRET="YOUR_QBO_CLIENT_SECRET",QBO_REDIRECT_URI="https://us-west2-your-project-id.cloudfunctions.net/qbo-callback",M365_CLIENT_ID="YOUR_M365_CLIENT_ID",M365_CLIENT_SECRET="YOUR_M365_CLIENT_SECRET",M365_REDIRECT_URI="https://us-west2-your-project-id.cloudfunctions.net/m365-callback",QBO_ENVIRONMENT="sandbox",TOUR_PRICE_PER_PERSON="25.00"
   ```

    > [!NOTE]
    > The endpoints will dynamically fetch QBO and Microsoft client IDs, secrets, and redirect URIs directly from the Firestore `config` collection (documents `qbo_auth` and `m365_auth`) if they are populated there. Setting them via `--set-env-vars` is optional and serves as a fallback.

#### B. Deploy `prune-unpaid-slots` (Secured Endpoint)
This function checks for expired unpaid bookings, cancels them, and removes calendar events. It is secured so that ONLY your Cloud Scheduler job can invoke it.
1. In the terminal, run the following command. Notice the lack of the `--allow-unauthenticated` flag, which locks it down:
   ```bash
   gcloud functions deploy prune-unpaid-slots \
     --runtime python310 \
     --trigger-http \
     --entry-point prune-unpaid-slugs \
     --region us-west2
   ```

### Step 5: Seed the Email Templates
1. Open your terminal.
2. Run the seeding script to upload the premium HTML receipt and reminder templates into Firestore:
   ```bash
   python seed_templates.py
   ```
3. Verify that the templates are successfully uploaded by checking the Firestore console under the `email_templates` collection.

### Step 6: Set Up QuickBooks Online (QBO) Developer App
1. Go to the [Intuit Developer Portal](https://developer.intuit.com/) and log in.
2. Click **My Apps** and select **Create an app**.
3. Select **QuickBooks Online API** and select the **Payments** and **Accounting** scopes.
4. Name your app `Bodie Tours` and select **Create app**.
5. Navigate to **Keys & credentials** in the sidebar.
6. Copy the **Client ID** and **Client Secret**.
7. In the **Redirect URIs** section, click **Add URI** and input:
   `https://us-west2-your-project-id.cloudfunctions.net/qbo-callback`
8. In the **Webhooks** section under **Production** or **Development**, configure your endpoint to point to:
   `https://us-west2-your-project-id.cloudfunctions.net/qbo-webhook`
   Subscribe to the `Invoice` webhook event.
10. In Firestore, create/update the document at `config/qbo_auth` with the following environment-specific and dynamic configuration fields:
    - **`environment`**: `"sandbox"` (or `"production"` for live environments).
    - **`callback_url`**: Your deployed QBO redirect URI (e.g., `https://us-west2-your-project-id.cloudfunctions.net/qbo-callback`).
    - **`dev-id`**: Your QuickBooks Sandbox Client ID.
    - **`dev-secret`**: Your QuickBooks Sandbox Client Secret.
    - **`dev-verifier_token`** (or fallback **`dev-verify`**): Your QuickBooks Sandbox Webhook Verifier Token.
    - **`prod-id`**: Your QuickBooks Production Client ID.
    - **`prod-secret`**: Your QuickBooks Production Client Secret.
    - **`prod-verifier_token`** (or fallback **`prod-verify`**): Your QuickBooks Production Webhook Verifier Token.
    This ensures that the application resolves the correct credentials dynamically depending on the current environment and prevents hardcoding credentials.



### Step 7: Set Up Microsoft Entra App (Azure AD)
1. Go to the [Microsoft Entra Admin Center](https://entra.microsoft.com/) (formerly Azure Active Directory).
2. Navigate to **Identity** -> **Applications** -> **App registrations** -> **New registration**.
3. Name the app `Bodie Park Tours`.
4. Under **Supported account types**, select **Accounts in any organizational directory (Any Microsoft Entra ID tenant - Multitenant) and personal Microsoft accounts**.
5. In the **Redirect URI (optional)** dropdown, select **Web** and type:
   `https://us-west2-your-project-id.cloudfunctions.net/m365-callback`
6. Click **Register**.
7. Copy the **Application (client) ID**.
8. Go to **Certificates & secrets** in the left sidebar, click **New client secret**, select an expiration duration, click **Add**, and copy the **Value** of the generated secret.
9. Go to **API permissions** -> **Add a permission** -> **Microsoft Graph** -> **Delegated permissions**. Select the following scopes:
   - `Calendars.ReadWrite`
   - `Mail.Send`
   - `offline_access` (required to obtain refresh tokens for background schedule runs)
10. Click **Add permissions**.
11. **(Optional) Configure a Specific Calendar**:
    By default, the system schedules tours on your account's primary calendar. To use a specific calendar (e.g. a separate "Bodie Tours" calendar):
    - Retrieve your list of calendar IDs by calling the Microsoft Graph API GET endpoint `https://graph.microsoft.com/v1.0/me/calendars` using your access token.
    - Locate the `id` of the calendar you wish to use from the JSON response.
    - In your Firestore database (`bodie-tours`), navigate to the `config` collection and select the `m365_auth` document.
    - Add or update the field **`calendar_id`** (Type: string) with the retrieved calendar ID value.
    - To switch back to the default calendar at any time, simply delete or clear the `calendar_id` field.


### Step 8: Configure the Cloud Scheduler Job for Pruning
1. In the search bar at the top of the Google Cloud Console, search for `Cloud Scheduler` and select **Cloud Scheduler**.
2. Click **Create Job**.
3. Configure the Scheduler job with the following parameters:
   - **Name**: `prune-unpaid-slots-job`
   - **Frequency**: `*/15 * * * *` (runs every 15 minutes)
   - **Timezone**: Choose your timezone.
   - **Target Type**: HTTP
   - **URL**: `https://us-west2-your-project-id.cloudfunctions.net/prune-unpaid-slugs`
   - **HTTP Method**: POST
   - **Auth Header**: Select **Add OIDC token**.
   - **Service Account**: Select the service account we created in Step 3 (`cloud-scheduler-invoker@your-project-id.iam.gserviceaccount.com`).
   - **Audience**: `https://us-west2-your-project-id.cloudfunctions.net/prune-unpaid-slugs`
4. Click **Create**.
Cloud Scheduler will now automatically call the pruning endpoint securely every 15 minutes, authenticating using OIDC.

### Step 9: Integrate the Booking Widget into Squarespace
Squarespace allows you to easily embed custom HTML/JavaScript elements. Follow these steps to embed the booking widget:

1. Log in to your [Squarespace Account](https://www.squarespace.com/) and open the site manager for your website.
2. Navigate to the page where you want the booking widget to appear (e.g., `Tours` or `Bookings`) and click **Edit** in the top left corner of the page preview.
3. Hover over the area where you want to place the widget, click the **+ Add Block** button (or an insert point marker), and select **Code** from the block menu.
4. Drag and size the Code Block to fit your page layout.
5. Click the **Edit (pencil)** icon on the Code Block.
6. In the dropdown menu for formatting, ensure it is set to **HTML**.
7. Make sure the **Display Source Code** toggle is turned **OFF**.
8. Open the local file `booking_widget.html` in a text editor (like Notepad, TextEdit, or VS Code).
9. Copy all of its contents (Ctrl+A then Ctrl+C).
10. In the Squarespace Code Block text editor, paste the contents (Ctrl+V).
11. **Important customization**: Locate the section of the pasted code where the API endpoint is defined, and change it from the local server to your deployed Google Cloud Function URL:
    - Look for:
      `const API_BASE_URL = 'http://localhost:8081';` or `const API_BASE_URL = 'https://us-west2-bodie-tours-prod.cloudfunctions.net/handle-booking';`
    - Change it to your actual deployed handle-booking URL:
      `const API_BASE_URL = 'https://us-west2-your-project-id.cloudfunctions.net/handle-booking';`
12. Click outside the block editor to close it.
13. Click **Done** -> **Save** in the top left corner of the Squarespace page editor.
14. View the page in your browser. The beautiful, responsive booking widget is now embedded and ready for customers!

<!-- GOAL_COMPLETE -->

