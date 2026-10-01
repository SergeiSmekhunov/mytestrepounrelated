"""
116117 Terminservice watcher (based on TorbenWetter/116117-terminservice-scraper).

Opens the search URL in headless Chrome, reads the result list and alerts by
email (Gmail SMTP) and/or Telegram. It never clicks anything that books.

Env (GitHub Actions secrets):
  BOOKING_URL          full search URL incl. code, PLZ and specialties
  RADIUS_KM            radius to enforce in the UI filter (default 50)
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
import smtplib
import sys
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

BOOKING_URL = os.environ["BOOKING_URL"]
RADIUS = os.getenv("RADIUS_KM", "50")
STATE_FILE = Path(os.getenv("STATE_FILE", ".state/last"))

NO_RESULTS = "Ihre Suche ergab leider keine Treffer"
RESULT_ITEM = ".search-results-item.ets-search-results-item"
CODE_PROBLEM_WORDS = ("ungültig", "abgelaufen", "bereits verwendet", "bereits eingelöst", "nicht gefunden")


def driver() -> Chrome:
    o = ChromeOptions()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
              "--disable-gpu", "--window-size=1920,2400", "--lang=de-DE"):
        o.add_argument(a)
    o.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    return Chrome(options=o)


def wait_spinner(d) -> None:
    try:
        WebDriverWait(d, 30).until(EC.invisibility_of_element_located((By.CSS_SELECTOR, ".loading-icon")))
    except TimeoutException:
        log.warning("spinner did not disappear")


def accept_cookies(d) -> None:
    try:
        WebDriverWait(d, 5).until(EC.element_to_be_clickable((
            By.XPATH, "//a[contains(@class,'cookies-info-close') and contains(.,'Auswahl bestätigen')]"
        ))).click()
        log.info("cookie banner closed (necessary only)")
    except TimeoutException:
        pass


def current_radius(d) -> str:
    try:
        return d.find_element(By.CSS_SELECTOR, ".ets-search-filter-distance .ets-search-filter-header .col-10 > span").text
    except Exception:
        return ""


def ensure_radius(d) -> None:
    """The URL param may not stick; click the radius bubble if needed."""
    if RADIUS in current_radius(d):
        return
    w = WebDriverWait(d, 20)
    try:
        w.until(EC.element_to_be_clickable((
            By.XPATH, "//div[contains(@class,'ets-search-filter-distance')]//div[contains(@class,'ets-search-filter-header')]"
        ))).click()
        bubbles = w.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".ets-search-filter-distance-bubbles")))
        bubbles.find_element(By.XPATH, f".//label[normalize-space(text())='{RADIUS}']").click()
        wait_spinner(d)
        log.info("radius now: %r", current_radius(d))
    except Exception as e:
        log.warning("could not set radius %s km (%s); continuing with %r", RADIUS, e, current_radius(d))


def wait_results(d) -> None:
    try:
        WebDriverWait(d, 40).until(EC.any_of(
            EC.presence_of_element_located((By.XPATH, f"//*[contains(text(),'{NO_RESULTS}')]")),
            EC.presence_of_element_located((By.CSS_SELECTOR, RESULT_ITEM)),
        ))
    except TimeoutException:
        pass


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
        wait_spinner(d)
        accept_cookies(d)
        ensure_radius(d)
        wait_results(d)
        d.save_screenshot(str(shot))

        page = d.find_element(By.TAG_NAME, "body").text
        items = [e.text.strip() for e in d.find_elements(By.CSS_SELECTOR, RESULT_ITEM) if e.text.strip()]

        if items and NO_RESULTS not in page:
            fp = hashlib.sha256("\n".join(items).encode()).hexdigest()
            if already_sent(fp):
                log.info("%d slot(s), unchanged since last alert", len(items))
                return 0
            body = "\n\n".join(items) + f"\n\nBook quickly: {BOOKING_URL}"
            alert("116117: appointments available", body, shot)
            remember(fp)
            return 0

        if NO_RESULTS in page:
            log.info("No appointments available")
            remember("none")  # so the next real slot list alerts again
            return 0

        low = page.lower()
        if any(w in low for w in CODE_PROBLEM_WORDS):
            fp = "code:" + hashlib.sha256(page.encode()).hexdigest()
            if not already_sent(fp):
                alert("116117: code problem", page[:2000], shot)
                remember(fp)
            return 0

        log.error("unexpected page (blocked or layout changed). First 800 chars:\n%s", page[:800])
        return 1
    finally:
        d.quit()


if __name__ == "__main__":
    sys.exit(main())
