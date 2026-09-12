import argparse
import json
import logging
import os
from datetime import datetime
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


TEACHER_ID = _required_env("PREFACE_TEACHER_ID")
SCRIPT_URL = _required_env("PREFACE_SCRIPT_URL")
CALENDAR_ID = _required_env("GOOGLE_CALENDAR_ID")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
TZ = ZoneInfo("Asia/Hong_Kong")
SYNC_SOURCE = "preface-sync"

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


def fetch_schedule():
    """Fetches class schedule from the Preface Apps Script API."""
    now = datetime.now(TZ)
    year = now.year
    month = now.month

    log.info("Fetching schedule for teacher %s, %d-%02d", TEACHER_ID, year, month)

    # The API expects: request=["getTeacherSchedule","[teacherId,year,month,true]",null,[0],null,null,1,0]
    args = json.dumps([int(TEACHER_ID), year, month, True])
    payload = {
        "request": json.dumps(["getTeacherSchedule", args, None, [0], None, None, 1, 0]),
    }

    response = requests.post(
        f"{SCRIPT_URL}/callback",
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Same-Domain": "1",
        },
    )
    response.raise_for_status()

    # Strip XSSI guard )]}'
    text = response.text.lstrip(")]}'\n")
    data = json.loads(text)

    # Unwrap: [["op.exec", [0, "{...json...}"]]]
    inner = json.loads(data[0][1][1])

    classes = inner.get("classes", [])
    admin_blocks = inner.get("adminBlocks", [])
    log.info("Received %d classes, %d admin blocks", len(classes), len(admin_blocks))

    events = []
    for cls in classes:
        start_dt = datetime(
            *map(int, cls["date"].split("-")),
            *map(int, cls["start"].split(":")),
            tzinfo=TZ,
        )
        end_dt = datetime(
            *map(int, cls["date"].split("-")),
            *map(int, cls["end"].split(":")),
            tzinfo=TZ,
        )

        events.append({
            "summary": f"{cls['course']} - {cls['students']}".strip(" -"),
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "location": cls.get("location", ""),
            "description": cls.get("note", "") or "",
            "type": "class",
        })

    # Skip admin blocks already auto-merged into a class — the class entry
    # carries the merged block's id in originalRequests[].blockId. Without
    # this guard we'd duplicate "Applied AI with Claude" once it's merged.
    merged_block_ids = {
        orig["blockId"]
        for cls in classes
        for orig in (cls.get("originalRequests") or [])
        if orig.get("blockId")
    }

    skipped_merged = 0
    for blk in admin_blocks:
        if blk.get("id") in merged_block_ids:
            skipped_merged += 1
            continue

        start_dt = datetime(
            *map(int, blk["date"].split("-")),
            *map(int, blk["start"].split(":")),
            tzinfo=TZ,
        )
        end_dt = datetime(
            *map(int, blk["date"].split("-")),
            *map(int, blk["end"].split(":")),
            tzinfo=TZ,
        )

        title = (blk.get("title") or "").strip()
        genre = (blk.get("genre") or "").strip()
        summary = f"{title} ({genre})" if genre else title

        events.append({
            "summary": summary,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "location": blk.get("location", ""),
            "description": blk.get("info", "") or "",
            "type": "admin",
        })

    if skipped_merged:
        log.info("Skipped %d admin block(s) already merged into a class", skipped_merged)

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
