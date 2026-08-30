# Outlook → Google Calendar (one-way, daily)

Mirrors your published Outlook calendar into a dedicated Google calendar once a
day, so Claude Code, Cowork and the desktop app can read your work schedule
through the Google Calendar connector.

Outlook stays the source of truth. Nothing is ever written back to it, and the
mirror calendar is kept separate from `1. DAV` so you can toggle it off in the
Google UI without losing your personal events.

## Before you start

Check that your tenant lets you publish a calendar at all. In Outlook web:

> Settings → Calendar → Shared calendars → **Publish a calendar**

If that section is missing or greyed out, anonymous calendar sharing is disabled
by policy and this approach has no source feed. Nothing below will help; see the
fallbacks at the end.

If it is available, choose a detail level, publish, and copy the **ICS** link
(not the HTML one).

Treat that link as a credential. Anyone holding it can read the feed, forever,
until you unpublish and republish. That is also the reason it lives in a
repository secret and never in a committed file.

## Setup

**1. Publish the Outlook calendar** and copy the ICS URL, as above.

**2. Create a Google OAuth client.** In the
[Google Cloud Console](https://console.cloud.google.com/): create a project,
enable the **Google Calendar API**, then under *Credentials* create an OAuth
client of type **Desktop app**. Note the client ID and client secret. While the
consent screen is in "Testing", add `davriko@gmail.com` as a test user.

**3. Get a refresh token**, once, on your own machine:

```bash
pip install -r scripts/requirements.txt
python3 scripts/google_oauth_setup.py --client-id XXX --client-secret YYY
```

It opens a browser, you approve, and it prints the refresh token.

**4. Add four repository secrets** under Settings → Secrets and variables →
Actions → *Secrets*:

| Secret | Value |
|---|---|
| `OUTLOOK_ICS_URL` | the published ICS link from step 1 |
| `GOOGLE_CLIENT_ID` | from step 2 |
| `GOOGLE_CLIENT_SECRET` | from step 2 |
| `GOOGLE_REFRESH_TOKEN` | from step 3 |

**5. Run it once by hand.** Actions → *Outlook → Google Calendar* → Run
workflow, with **dry run** ticked. That reports what it would create without
writing anything. If the counts look sane, run it again unticked. The mirror
calendar is created automatically on the first real run.

After that it runs itself at 23:00 UTC — 06:00 in Bangkok, so the day's
schedule is current before you start.

## Tuning

Optional repository *variables* (same settings page, *Variables* tab):

| Variable | Default | Meaning |
|---|---|---|
| `TARGET_CALENDAR_SUMMARY` | `Outlook (mirror)` | name of the mirror calendar |
| `MIRROR_DETAIL` | `full` | `busy` mirrors only timing — no titles, locations or descriptions |
| `MIRROR_TIME_ZONE` | `Asia/Bangkok` | time zone for the created calendar |
| `WINDOW_DAYS_BACK` | `7` | how much history to keep |
| `WINDOW_DAYS_AHEAD` | `180` | how far forward to mirror |

`MIRROR_DETAIL=busy` is worth considering: it gives Claude the shape of your
week for planning and scheduling without FAO meeting titles and attendee notes
being copied into a personal Google account.

To sync more often than daily, edit the `cron` line in
`.github/workflows/outlook-to-google-calendar.yml`. Outlook regenerates the
published feed on its own schedule, so more than hourly buys little.

## How it behaves

- **Recurring meetings are expanded** into individual occurrences, so Outlook's
  own exceptions — a moved standup, a cancelled instance — appear exactly as
  Outlook resolved them.
- **Reruns are idempotent.** Each event carries a content hash; unchanged events
  are left alone, changed ones are updated in place, and events that vanished
  from Outlook are deleted from the mirror.
- **Only its own events are touched.** Every mirrored event is tagged with a
  private property, and the script updates or deletes nothing else. It also
  refuses to write into your primary calendar unless `ALLOW_PRIMARY=true`.
- **No invitations are ever sent.** Attendees and the organizer are deliberately
  not copied, because Google would email them from your personal account.
- **Reminders are suppressed** on mirrored events, so you are not notified twice
  for the same meeting.

## Running it locally

```bash
export OUTLOOK_ICS_URL='https://outlook.office365.com/owa/calendar/.../calendar.ics'
DRY_RUN=1 python3 scripts/outlook_to_google_calendar.py
```

With `DRY_RUN=1` and no Google credentials it just prints what it found in the
feed — the quickest way to confirm the ICS link works.

## If publishing is blocked by policy

- **One-off export.** Outlook → Save Calendar → `.ics`, then import into Google.
  A frozen snapshot, no sync, but fine before a mission.
- **Forward invites to Gmail.** Gmail creates events from invitation emails, so
  forwarding an invite lands it in Google with no admin toggle involved. Do this
  by hand — auto-forwarding rules to external addresses are usually blocked and
  routinely audited.
