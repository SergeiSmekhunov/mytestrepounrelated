"""
116117 Terminservice watcher (inspired by TorbenWetter/116117-terminservice-scraper,
adapted to the current 116117-termine.de page).

Opens the search URL in headless Chrome, reads "N Termine im Umkreis von X km"
and the result list, and alerts by email (Gmail SMTP) and/or Telegram.
It never clicks anything that books.

Env (GitHub Actions secrets):
  BOOKING_URL          full search URL incl. code, PLZ, specialties and ?suchradius=
  SMTP_USER            Gmail address that sends the mail
  SMTP_APP_PASSWORD    Gmail app password (not your normal password)
  MAIL_TO              comma-separated recipients
  TELEGRAM_TOKEN       optional
  TELEGRAM_CHAT_ID     optional
  STATE_FILE           where the last-alerted fingerprint lives (default .state/last)

Exit codes: 0 = ran fine (slots or not), 1 = page did not load as expected.
"""

import hashlib
import logging
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from selenium.common.exceptions import TimeoutException
from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("116117")

BOOKING_URL = os.getenv("BOOKING_URL", "").strip()
if not BOOKING_URL:
    log.warning("BOOKING_URL secret not set yet; skipping run")
    sys.exit(0)

STATE_FILE = Path(os.getenv("STATE_FILE", ".state/last"))

# e.g. "0 TERMINE IM UMKREIS VON 50 KM", "1 TERMIN IM UMKREIS VON 50 KM"
COUNT_RE = re.compile(r"(\d+)\s+TERMINE?\s+IM\s+UMKREIS\s+VON\s+(\d+)\s*KM", re.I)
# Text after the result list starts here; used to cut out the slot listing.
END_MARKERS = ("Was Sie jetzt tun können", "Support:")
NOISE_LINES = {"Umkreis erweitern", "Filtern"}
CODE_PROBLEM_WORDS = ("ungültig", "abgelaufen", "bereits verwendet", "bereits eingelöst", "nicht gefunden")


def driver() -> Chrome:
    o = ChromeOptions()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
              "--disable-gpu", "--window-size=1920,2400", "--lang=de-DE"):
        o.add_argument(a)
    o.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    return Chrome(options=o)


def body_text(d) -> str:
    try:
        return d.find_element(By.TAG_NAME, "body").text
    except Exception:
        return ""


def accept_cookies(d) -> None:
    """Close the cookie banner with only necessary cookies, if one shows up."""
    for xp in ("//button[contains(.,'Nur notwendige')]", "//a[contains(.,'Nur notwendige')]",
               "//button[contains(.,'Auswahl bestätigen')]", "//a[contains(.,'Auswahl bestätigen')]"):
        try:
            WebDriverWait(d, 2).until(EC.element_to_be_clickable((By.XPATH, xp))).click()
            log.info("cookie banner closed")
            return
        except TimeoutException:
            continue


def wait_for_outcome(d, timeout: int = 45) -> str:
    """Wait until the result count (or a code error) is rendered; return page text."""
    end = time.time() + timeout
    text = ""
    while time.time() < end:
        text = body_text(d)
        if COUNT_RE.search(text) or any(w in text.lower() for w in CODE_PROBLEM_WORDS):
            time.sleep(2)  # let the list finish rendering after the count appears
            return body_text(d)
        time.sleep(1)
    return text


def slot_section(text: str, count_match: re.Match) -> str:
    """Text between the count line and the 'what you can do now' block."""
    rest = text[count_match.end():]
    cut = min((i for i in (rest.find(m) for m in END_MARKERS) if i >= 0), default=len(rest))
    lines = [l.strip() for l in rest[:cut].splitlines()]
    return "\n".join(l for l in lines if l and l not in NOISE_LINES)


def send_email(subject: str, body: str) -> None:
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_APP_PASSWORD"), os.getenv("MAIL_TO")
    if not (user and pw and to):
        log.info("email not configured, skipping")
        return
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = user, to, subject
    m.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(m)
    log.info("email sent to %s", to)


def send_telegram(text: str, screenshot: Path | None) -> None:
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        log.info("telegram not configured, skipping")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20)
    if screenshot and screenshot.exists():
        import requests  # only needed for multipart upload
        requests.post(f"https://api.telegram.org/bot{tok}/sendPhoto",
                      data={"chat_id": chat}, files={"photo": screenshot.open("rb")}, timeout=30)
    log.info("telegram sent")


def alert(subject: str, body: str, screenshot: Path | None = None) -> None:
    errors = []
    for fn in (lambda: send_email(subject, body), lambda: send_telegram(f"{subject}\n\n{body}", screenshot)):
        try:
            fn()
        except Exception as e:  # keep going so one channel failing doesn't kill the other
            errors.append(e)
            log.error("notify failed: %s", e)
    if errors:
        raise RuntimeError(f"{len(errors)} notification channel(s) failed")


def already_sent(fp: str) -> bool:
    return STATE_FILE.exists() and STATE_FILE.read_text().strip() == fp


def remember(fp: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(fp)


def main() -> int:
    d = driver()
    shot = Path("screenshot.png")
    try:
        d.get(BOOKING_URL)
        accept_cookies(d)
        page = wait_for_outcome(d)
        d.save_screenshot(str(shot))

        m = COUNT_RE.search(page)
        if m:
            count, radius = int(m.group(1)), m.group(2)
            log.info("%d appointment(s) within %s km", count, radius)
            if count == 0:
                remember("none")  # so the next real slot list alerts again
                return 0
            slots = slot_section(page, m)
            fp = hashlib.sha256(slots.encode()).hexdigest()
            if already_sent(fp):
                log.info("slot list unchanged since last alert")
                return 0
            body = (f"{count} appointment(s) within {radius} km:\n\n{slots}\n\n"
                    f"Book quickly: {BOOKING_URL}")
            log.info("alerting:\n%s", body)
            alert("116117: appointments available", body, shot)
            remember(fp)
            return 0

        if any(w in page.lower() for w in CODE_PROBLEM_WORDS):
            fp = "code:" + hashlib.sha256(page.encode()).hexdigest()
            if not already_sent(fp):
                alert("116117: code problem", page[:2000], shot)
                remember(fp)
            return 0

        log.error("unexpected page (blocked or layout changed)")
        log.error("URL now: %s | title: %r", d.current_url, d.title)
        log.error("Page text:\n%s", page[:1500] or "(empty body)")
        return 1
    finally:
        d.quit()


if __name__ == "__main__":
    sys.exit(main())
