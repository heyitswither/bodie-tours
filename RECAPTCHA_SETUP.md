# Google reCAPTCHA v3 Setup Guide

This document describes how to configure Google reCAPTCHA v3 security for the Bodie Tours Booking Widget and backend system.

---

## 1. Overview
reCAPTCHA v3 helps detect and block abusive traffic and automated spam bots on the booking form without interrupting real users with interactive challenges. 
It uses a risk score (0.0 to 1.0) to assess user actions, where `1.0` is very likely a human and `0.0` is very likely a bot.
Our backend (`main.py`) validates this token against Google's verification servers using a threshold of `0.5`.

---

## 2. Generate Credentials from Google reCAPTCHA Console

To obtain the required credentials for your domain:

1. Visit the [Google reCAPTCHA Admin Console](https://www.google.com/recaptcha/admin).
2. Register a new site:
   - **Label**: e.g., `Bodie Tours`
   - **reCAPTCHA type**: Choose **reCAPTCHA v3**.
   - **Domains**: Add your production domain (e.g., `bodiefoundation.org`, `www.bodiefoundation.org`) and development domains (e.g., `localhost`).
3. Accept the Terms of Service and click **Submit**.
4. Copy the generated keys:
   - **Site Key**: Used in the frontend/HTML code to load the script and generate user action tokens.
   - **Secret Key**: Used by the backend to verify the tokens with Google.

---

## 3. Configuration

The system is designed to load credentials dynamically. You can configure them either using Firestore or Environment Variables.

### Option A: Firestore Configuration (Recommended)
This is the preferred option for cloud deployments, allowing keys to be updated at runtime without redeploying code.

Store the keys in Cloud Firestore with the following details:
- **Collection**: `config`
- **Document ID**: `recaptcha_auth`
- **Fields**:
  - `site_key` (String): Your Google reCAPTCHA Site Key.
  - `secret_key` (String): Your Google reCAPTCHA Secret Key.

### Option B: Environment Variables
You can also supply the credentials as environment variables in your server configuration (e.g., in Cloud Functions or your local shell):

```bash
export RECAPTCHA_SITE_KEY="your_site_key_here"
export RECAPTCHA_SECRET_KEY="your_secret_key_here"
```

*Note: Environment variables will take precedence over Firestore configurations.*

---

## 4. Automatic Development Bypass

To facilitate seamless local offline development and testing, **reCAPTCHA verification is bypassed automatically if the Secret Key is not configured** (meaning it is missing or empty in both environment variables and the Firestore `config/recaptcha_auth` document).

- When the secret key is missing, the backend prints a bypass log:
  ```
  [reCAPTCHA] Secret key not configured. Bypassing verification.
  ```
- This allows local unit tests and developer environments to submit bookings out-of-the-box without requiring active Google API credentials or network connections.
