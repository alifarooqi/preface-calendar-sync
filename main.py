import argparse
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIGURATION ---
def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is required. "
            f"Set it via shell export, a sourced .env file, or GitHub Actions secrets."
        )
    return value


PREFACE_LOGIN = _required_env("PREFACE_LOGIN")
PREFACE_PASSWORD = _required_env("PREFACE_PASSWORD")
CALENDAR_ID = _required_env("GOOGLE_CALENDAR_ID")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
TZ = ZoneInfo("Asia/Hong_Kong")
SYNC_SOURCE = "preface-sync"

PORTAL_BASE = "https://portal.preface.ai"
NOMAD_ORIGIN = "https://nomad.preface.ai"
LOGIN_PATH = "/api/np/users/login"
PROFILE_PATH = "/api/np/user/profile"
CALENDAR_PATH = "/api/np/nomad/show_calendar"
SYNC_DAYS = 60


def _nomad_headers(jwt=None):
    """Common headers for portal.preface.ai calls. Adds Bearer auth when jwt given."""
    headers = {
        "content-type": "application/json; charset=utf-8",
        "preface-client": "np-web",
        "origin": NOMAD_ORIGIN,
        "referer": f"{NOMAD_ORIGIN}/",
    }
    if jwt:
        headers["authorization"] = f"Bearer {jwt}"
    return headers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def get_calendar_service():
    """Authenticates as a service account using service-account.json."""
    credentials = service_account.Credentials.from_service_account_file(
        "service-account.json",
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=credentials)


def _login():
    """POST /api/np/users/login and return the JWT from the Authorization response header."""
    log.info("Logging in to portal.preface.ai as %s", PREFACE_LOGIN)
    response = requests.post(
        f"{PORTAL_BASE}{LOGIN_PATH}",
        json={"user": {"login": PREFACE_LOGIN, "password": PREFACE_PASSWORD}},
        headers=_nomad_headers(),
        timeout=30,
    )
    response.raise_for_status()

    auth = response.headers.get("authorization") or response.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise RuntimeError(
            "Login succeeded but no Bearer token found in response headers. "
            "The portal API may have changed."
        )
    return auth[7:]


def _get_user_id(jwt):
    """GET /api/np/user/profile and return the user's numeric id."""
    response = requests.get(
        f"{PORTAL_BASE}{PROFILE_PATH}",
        headers=_nomad_headers(jwt),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"]["id"]


def fetch_schedule():
    """Fetches the next SYNC_DAYS of booked classes from the Preface portal API."""
    now = datetime.now(TZ)
    start_date = now.date().isoformat()
    end_date = (now + timedelta(days=SYNC_DAYS)).date().isoformat()

    jwt = _login()
    user_id = _get_user_id(jwt)
    log.info("Fetching schedule for user %s, %s → %s", user_id, start_date, end_date)

    response = requests.get(
        f"{PORTAL_BASE}{CALENDAR_PATH}",
        params={"user_id": user_id, "start_date": start_date, "end_date": end_date},
        headers=_nomad_headers(jwt),
        timeout=30,
    )
    response.raise_for_status()

    schedules = response.json().get("data", {}).get("schedules", [])
    log.info("Received %d booked class(es)", len(schedules))

    events = []
    for s in schedules:
        events.append({
            "summary": f"{s.get('title', '')} - {s.get('student_name', '')}".strip(" -"),
            "start": s["time"],
            "end": s["endTime"],
            "location": s.get("address", "") or "",
            "description": s.get("admin_remarks") or "",
            "type": "class",
        })

    if not events:
        log.warning("No events returned from the API.")

    return events


def _month_range(year, month):
    """Return (time_min, time_max) ISO strings for a given calendar month."""
    time_min = datetime(year, month, 1, 0, 0, 0, tzinfo=TZ).isoformat()
    if month == 12:
        time_max = datetime(year + 1, 1, 1, 23, 59, 59, tzinfo=TZ).isoformat()
    else:
        time_max = datetime(year, month + 1, 1, 23, 59, 59, tzinfo=TZ).isoformat()
    return time_min, time_max


def clear_synced_events(service, events, dry_run=False):
    """Deletes previously synced events for all months covered by the given events."""
    months = set()
    for ev in events:
        dt = datetime.fromisoformat(ev["start"])
        months.add((dt.year, dt.month))

    for year, month in months:
        time_min, time_max = _month_range(year, month)
        log.info("Clearing synced events for %d-%02d...", year, month)

        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            privateExtendedProperty=f"source={SYNC_SOURCE}",
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        for event in events_result.get("items", []):
            summary = event.get("summary", "")
            log.info("Deleting stale event: %s (%s)", summary, event["start"].get("dateTime"))
            if not dry_run:
                service.events().delete(calendarId=CALENDAR_ID, eventId=event["id"]).execute()


def sync_to_google_calendar(events, service, dry_run=False):
    """Pushes events into Google Calendar, clearing old synced entries first."""
    if not events:
        log.info("No events found to sync.")
        return

    clear_synced_events(service, events, dry_run=dry_run)

    log.info("Writing %d events into Google Calendar...", len(events))
    for ev in events:
        event_body = {
            "summary": ev["summary"],
            "location": ev["location"],
            "start": {"dateTime": ev["start"], "timeZone": "Asia/Hong_Kong"},
            "end": {"dateTime": ev["end"], "timeZone": "Asia/Hong_Kong"},
            "extendedProperties": {
                "private": {
                    "source": SYNC_SOURCE,
                    "type": ev.get("type", "class"),
                },
            },
        }
        description = ev.get("description") or ""
        if description:
            event_body["description"] = description
        if dry_run:
            desc = ev.get("description") or ""
            log.info(
                "[DRY RUN] Would create: %s at %s%s",
                ev["summary"],
                ev["start"],
                f"\n    {desc}" if desc else "",
            )
        else:
            try:
                created_event = service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
                log.info("Created: %s at %s", created_event.get("summary"), ev["start"])
            except Exception as e:
                log.error("Failed to create event %r: %s", ev["summary"], e)


def main():
    arg_parser = argparse.ArgumentParser(description="Sync Preface class schedule to Google Calendar")
    arg_parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying the calendar")
    args = arg_parser.parse_args()

    service = get_calendar_service()
    events = fetch_schedule()
    sync_to_google_calendar(events, service, dry_run=args.dry_run)
    log.info("Sync complete!")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error("An error occurred: %s", e)
        raise
