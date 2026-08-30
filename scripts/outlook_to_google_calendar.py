#!/usr/bin/env python3
"""Mirror a published Outlook ICS feed into a dedicated Google Calendar.

One-way only: Outlook is the source of truth, Google is a read-only copy that
Claude (Claude Code, Cowork, the desktop app) can read through the Google
Calendar connector. Nothing is ever written back to Outlook.

The mirror is idempotent. Every event this script creates is tagged with a
private extended property; only tagged events are ever updated or deleted, so
a misconfigured calendar id can never damage a hand-maintained calendar.

Configuration is entirely by environment variable -- see docs/outlook-to-google-sync.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests
from icalendar import Calendar

import recurring_ical_events

# Marks every event this script owns. Do not change it without clearing the
# mirror calendar first: previously created events would become unmanaged.
MIRROR_TAG = "outlook-mirror-v1"

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"


class ConfigError(RuntimeError):
    pass


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default if default is not None else "")
    if required and not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# Source feed
# --------------------------------------------------------------------------

def fetch_ics(source: str) -> bytes:
    """Read the ICS feed from an http(s)/webcal URL or a local path."""
    if source.startswith("webcal://"):
        source = "https://" + source[len("webcal://") :]

    if source.startswith(("http://", "https://")):
        response = requests.get(source, timeout=60)
        response.raise_for_status()
        return response.content

    path = source[len("file://") :] if source.startswith("file://") else source
    with open(path, "rb") as handle:
        return handle.read()


def as_aware(value: datetime | date, fallback_tz: timezone) -> datetime | date:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=fallback_tz)
    return value


def google_time(value: datetime | date) -> dict:
    """All-day events use `date`; timed events use RFC3339 `dateTime`."""
    if isinstance(value, datetime):
        return {"dateTime": value.isoformat()}
    return {"date": value.isoformat()}


def text(component, key: str) -> str:
    raw = component.get(key)
    return "" if raw is None else str(raw).strip()


def build_desired_events(ics_bytes: bytes, window_start: datetime,
                         window_end: datetime, detail: str) -> dict[str, dict]:
    """Expand the feed across the window into one Google payload per occurrence.

    Recurrences are expanded here rather than passed through as RRULEs so that
    Outlook's own exceptions -- a moved standup, a cancelled instance -- are
    reflected exactly as Outlook resolved them.
    """
    calendar = Calendar.from_ical(ics_bytes)
    occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)

    desired: dict[str, dict] = {}
    for component in occurrences:
        if text(component, "STATUS").upper() == "CANCELLED":
            continue

        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue
        start = as_aware(dtstart.dt, timezone.utc)

        dtend = component.get("DTEND")
        end = as_aware(dtend.dt, timezone.utc) if dtend is not None else None
        if end is None:
            end = start + timedelta(minutes=15) if isinstance(start, datetime) else start + timedelta(days=1)
        if isinstance(start, datetime) and isinstance(end, datetime) and end <= start:
            end = start + timedelta(minutes=15)

        uid = text(component, "UID") or "no-uid"
        key = hashlib.sha1(
            f"{uid}|{start.isoformat()}".encode("utf-8")
        ).hexdigest()

        summary = text(component, "SUMMARY") or "(no title)"
        payload: dict = {
            "summary": summary if detail == "full" else "Busy",
            "start": google_time(start),
            "end": google_time(end),
            # Never copy attendees or the organizer: Google would email them,
            # which would send invitations from a personal account for meetings
            # that live in the work tenant.
            "visibility": "private",
            "transparency": "transparent"
                if text(component, "TRANSP").upper() == "TRANSPARENT" else "opaque",
            "reminders": {"useDefault": False, "overrides": []},
        }
        if detail == "full":
            location = text(component, "LOCATION")
            description = text(component, "DESCRIPTION")
            if location:
                payload["location"] = location
            if description:
                payload["description"] = description

        payload["extendedProperties"] = {
            "private": {
                "mirrorTag": MIRROR_TAG,
                "mirrorKey": key,
                "mirrorHash": content_hash(payload),
            }
        }
        # Later duplicates of the same key are the same occurrence.
        desired.setdefault(key, payload)

    return desired


def content_hash(payload: dict) -> str:
    """Hash of the fields we actually mirror, used to skip unchanged events."""
    comparable = {k: v for k, v in payload.items() if k != "extendedProperties"}
    encoded = json.dumps(comparable, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Google Calendar API
# --------------------------------------------------------------------------

class GoogleCalendar:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.session = requests.Session()
        self.access_token = self._access_token(client_id, client_secret, refresh_token)

    def _access_token(self, client_id: str, client_secret: str, refresh_token: str) -> str:
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(
                "could not exchange the refresh token for an access token "
                f"({response.status_code}). Check GOOGLE_CLIENT_ID / "
                f"GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN. Response: {response.text[:400]}"
            )
        return response.json()["access_token"]

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"

        for attempt in range(5):
            response = self.session.request(method, url, headers=headers, timeout=60, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return response
        return response

    def json(self, method: str, path: str, **kwargs) -> dict:
        response = self.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} failed ({response.status_code}): {response.text[:400]}")
        return response.json() if response.content else {}

    def calendar_list(self) -> list[dict]:
        items, page_token = [], None
        while True:
            params = {"maxResults": 250, "showHidden": "true"}
            if page_token:
                params["pageToken"] = page_token
            data = self.json("GET", "/users/me/calendarList", params=params)
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return items

    def create_calendar(self, summary: str, time_zone: str) -> dict:
        return self.json("POST", "/calendars",
                         json={"summary": summary, "timeZone": time_zone})

    def tagged_events(self, calendar_id: str) -> dict[str, dict]:
        """Every event this script owns, regardless of date.

        Deliberately unbounded in time: an event that Outlook moved outside the
        sync window still has to be found so it can be removed.
        """
        found: dict[str, dict] = {}
        page_token = None
        while True:
            params = {
                "privateExtendedProperty": f"mirrorTag={MIRROR_TAG}",
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": 2500,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self.json("GET", f"/calendars/{quote(calendar_id)}/events", params=params)
            for item in data.get("items", []):
                key = item.get("extendedProperties", {}).get("private", {}).get("mirrorKey")
                if key:
                    found[key] = item
            page_token = data.get("nextPageToken")
            if not page_token:
                return found

    def insert_event(self, calendar_id: str, payload: dict) -> None:
        self.json("POST", f"/calendars/{quote(calendar_id)}/events", json=payload)

    def update_event(self, calendar_id: str, event_id: str, payload: dict) -> None:
        self.json("PUT", f"/calendars/{quote(calendar_id)}/events/{quote(event_id)}", json=payload)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        response = self.request("DELETE", f"/calendars/{quote(calendar_id)}/events/{quote(event_id)}")
        # 410 means it is already gone, which is the state we wanted anyway.
        if response.status_code >= 400 and response.status_code != 410:
            raise RuntimeError(f"deleting {event_id} failed ({response.status_code}): {response.text[:200]}")


def resolve_target_calendar(api: GoogleCalendar, summary: str,
                            explicit_id: str, time_zone: str,
                            allow_primary: bool) -> str:
    entries = api.calendar_list()
    primary_id = next((c["id"] for c in entries if c.get("primary")), None)

    if explicit_id:
        target_id = "primary" if explicit_id == "primary" else explicit_id
    else:
        match = next((c for c in entries if c.get("summary") == summary), None)
        if match:
            target_id = match["id"]
        else:
            print(f"creating mirror calendar {summary!r}")
            target_id = api.create_calendar(summary, time_zone)["id"]

    is_primary = target_id == "primary" or (primary_id and target_id == primary_id)
    if is_primary and not allow_primary:
        raise ConfigError(
            "refusing to write into your primary calendar. Mirrored events belong "
            "in their own calendar so you can toggle them off and so a bad run can "
            "never touch events you created by hand. Set ALLOW_PRIMARY=true only if "
            "you really mean it."
        )
    return target_id


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    try:
        ics_url = env("OUTLOOK_ICS_URL", required=True)
        detail = env("MIRROR_DETAIL", "full").lower()
        if detail not in {"full", "busy"}:
            raise ConfigError("MIRROR_DETAIL must be 'full' or 'busy'")

        days_back = env_int("WINDOW_DAYS_BACK", 7)
        days_ahead = env_int("WINDOW_DAYS_AHEAD", 180)
        time_zone = env("MIRROR_TIME_ZONE", "Asia/Bangkok")
        dry_run = env_flag("DRY_RUN")

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days_back)
        window_end = now + timedelta(days=days_ahead)

        print(f"reading {ics_url.split('?')[0]} …")
        ics_bytes = fetch_ics(ics_url)
        desired = build_desired_events(ics_bytes, window_start, window_end, detail)
        print(f"{len(desired)} occurrences in window "
              f"{window_start.date()} → {window_end.date()} (detail: {detail})")

        client_id = env("GOOGLE_CLIENT_ID")
        if dry_run and not client_id:
            for payload in list(desired.values())[:20]:
                print(f"  would mirror: {payload['start']} {payload['summary']}")
            if len(desired) > 20:
                print(f"  … and {len(desired) - 20} more")
            return 0

        api = GoogleCalendar(
            client_id=env("GOOGLE_CLIENT_ID", required=True),
            client_secret=env("GOOGLE_CLIENT_SECRET", required=True),
            refresh_token=env("GOOGLE_REFRESH_TOKEN", required=True),
        )
        calendar_id = resolve_target_calendar(
            api,
            summary=env("TARGET_CALENDAR_SUMMARY", "Outlook (mirror)"),
            explicit_id=env("TARGET_CALENDAR_ID"),
            time_zone=time_zone,
            allow_primary=env_flag("ALLOW_PRIMARY"),
        )
        print(f"target calendar: {calendar_id}")

        existing = api.tagged_events(calendar_id)
        created = updated = deleted = unchanged = 0

        for key, payload in desired.items():
            current = existing.get(key)
            if current is None:
                if not dry_run:
                    api.insert_event(calendar_id, payload)
                created += 1
            elif current.get("extendedProperties", {}).get("private", {}).get("mirrorHash") \
                    != payload["extendedProperties"]["private"]["mirrorHash"]:
                if not dry_run:
                    api.update_event(calendar_id, current["id"], payload)
                updated += 1
            else:
                unchanged += 1

        for key, current in existing.items():
            if key not in desired:
                if not dry_run:
                    api.delete_event(calendar_id, current["id"])
                deleted += 1

        prefix = "would " if dry_run else ""
        summary_line = (f"{prefix}created {created}, {prefix}updated {updated}, "
                        f"{prefix}deleted {deleted}, unchanged {unchanged}")
        print(summary_line)

        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write(f"### Outlook → Google\n\n{summary_line}\n")
        return 0

    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface a readable failure in CI logs
        print(f"sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
