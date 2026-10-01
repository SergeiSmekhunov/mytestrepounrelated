# 116117 watch

Checks the 116117 Terminservice about every 2 minutes on GitHub Actions (a run starts every 15 min and loops) and alerts by
email and Telegram when slots appear. Never books anything.
Based on [TorbenWetter/116117-terminservice-scraper](https://github.com/TorbenWetter/116117-terminservice-scraper).

## Setup

1. Push this folder to a new GitHub repo. **Public is recommended**: Actions minutes
   are free and unlimited there, while a private repo's 2,000 free min/month runs out
   at a 5-minute cadence. Nothing sensitive is in the code; the code and passwords
   live in secrets.
2. Settings → Secrets and variables → Actions → add:

   | Secret | Value |
   |---|---|
   | `BOOKING_URL` | `https://www.116117-termine.de/termin/suchen/<CODE>/<PLZ>/<SPECIALTIES>?suchradius=50` |
   | `SMTP_USER` | Gmail address that sends |
   | `SMTP_APP_PASSWORD` | Gmail **app password** (Google account → Security → App passwords; needs 2FA) |
   | `MAIL_TO` | `a@example.com,b@example.com` |
   | `TELEGRAM_TOKEN` | optional, from @BotFather |
   | `TELEGRAM_CHAT_ID` | optional |

3. Actions tab → "116117 check" → **Run workflow** once to test.

## Behaviour

- Slots found → one email + Telegram message listing them (with screenshot on Telegram).
  Repeats only when the slot list changes.
- Code invalid/expired → one "code problem" alert.
- No slots → silent.
- Page didn't load / was blocked → the run fails (red X); GitHub emails you about
  failed scheduled runs, and the screenshot is attached to the run.

## Notes

- GitHub's cron is best effort and can start late; the in-run loop covers most of that.
- Scheduled workflows on public repos pause after 60 days without commits; GitHub
  emails before that and a single "Enable workflow" click restarts them.
- Radius comes from `?suchradius=` in `BOOKING_URL`; the site honours it.
