# preface-calendar-sync

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Syncs your Preface class schedule into a Google Calendar on a daily cron.

## Setup (forked repo)

### 1. Fork & enable Actions
1. Fork this repo to your GitHub account.
2. In your fork, go to **Actions** and enable workflows.

### 2. Create a Google service account
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Library** → enable the **Google Calendar API**.
2. **IAM & Admin** → **Service Accounts** → **Create service account**. No role needed.
3. Open the account → **Keys** → **Add key** → **Create new key** → **JSON**. Save the file.
4. Base64-encode it for GitHub:
   ```sh
   base64 -i service-account.json | tr -d '\n'
   ```
   Copy the output — you'll paste it as a secret.

### 3. Create a new Google Calendar
1. Open [Google Calendar](https://calendar.google.com/) → click **+** next to "Other calendars" → **Create new calendar**.
2. Name it (e.g. "Preface") and create it.
3. Click the new calendar's ⋮ menu → **Settings and sharing** → scroll to **Integrate calendar** → copy the **Calendar ID** (looks like `abc123@group.calendar.google.com`).

### 4. Share the calendar with the service account
In the same **Settings and sharing** page → **Share with specific people** → add the service account's `email` (found in the JSON file) with **Make changes to events** permission.

### 5. Add GitHub repo secrets
In your fork: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add:

| Name | Value |
|------|-------|
| `SERVICE_ACCOUNT_JSON_B64` | The base64 string from step 2 |
| `PREFACE_LOGIN` | Your Preface email (used to log into the portal) |
| `PREFACE_PASSWORD` | Your Preface password |
| `GOOGLE_CALENDAR_ID` | The calendar ID from step 3 |

### 6. Trigger a run
**Actions** → **Preface Calendar Sync** → **Run workflow**. Classes should appear in your calendar within a minute.

## Run locally

Create a `.env`:
```
PREFACE_LOGIN=???
PREFACE_PASSWORD=???
GOOGLE_CALENDAR_ID=???
```

Place your downloaded `service-account.json` in the repo root, then:
```sh
pip install -r requirements.txt
set -a; source .env; set +a && python3 main.py
```

Add `--dry-run` to preview without modifying the calendar.