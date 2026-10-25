import os
import re
import sys
import time
import random
import logging
from datetime import datetime
 
import pytz
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
 
# --- SETTINGS (environment variables, never hard-code or share these) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")   # https://xxxx.supabase.co
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")               # service_role key (server-side only!)
 
AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")
QUIET_START = 22
QUIET_END = 6
 
# --- SEARCH: Amsterdam, 10 km radius, max EUR 900 ---
CITY = "amsterdam"
RADIUS_KM = 10
MAX_RENT = 900
 
SEARCH_URL = (
    f"https://kamernet.nl/en/for-rent/properties-{CITY}"
    f"?pageNo=1&radius={RADIUS_KM}&minSize=0&maxRent={MAX_RENT}&searchView=1&sort=1"
    "&availabilityPeriods=1&hasInternet=false&isBathroomPrivate=false"
    "&isKitchenPrivate=false&isToiletPrivate=false&suitableForNumberOfPersons=0"
    "&isSmokingInsideAllowed=false&isPetsInsideAllowed=false"
    "&nwlat=54.216270703936516&nwlng=-3.267085312500001"
    "&selat=50.130263513834905&selng=12.5532271875"
    "&mapZoom=7&mapMarkerLat=0&mapMarkerLng=0"
)
 
RUN_ONCE = "--once" in sys.argv
# --baseline: store every current listing in Supabase WITHOUT sending Telegram messages.
# Run this once at the start so you don't get flooded with old listings.
BASELINE = "--baseline" in sys.argv
 
# In-memory cache for this process only, to avoid asking Supabase about the same URL every cycle.
# Supabase stays the source of truth, so a restart loses nothing.
known_urls = set()      # already in Supabase
skipped_urls = set()    # over budget, never stored
 
 
# ---------------------------------------------------------------- Supabase
def sb_headers(extra=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }
    # Old-style keys (service_role) are JWTs and go in Authorization too.
    # New-style keys (sb_secret_...) are not JWTs and only go in the apikey header.
    if SUPABASE_KEY.startswith("eyJ"):
        h["Authorization"] = f"Bearer {SUPABASE_KEY}"
    if extra:
        h.update(extra)
    return h
 
 
def listing_exists(url):
    """True/False if Supabase answers, None if the lookup failed."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/listings",
            headers=sb_headers(),
            params={"select": "url", "url": f"eq.{url}", "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        return len(r.json()) > 0
    except Exception as e:
        logging.error("Supabase lookup failed for %s: %s", url, e)
        return None
 
 
def save_listing(url, title):
    """Insert url + title. found_at is filled in by the database (DEFAULT NOW()).
    Duplicates are ignored thanks to the UNIQUE constraint on url."""
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/listings",
            headers=sb_headers({"Prefer": "resolution=ignore-duplicates,return=minimal"}),
            params={"on_conflict": "url"},
            json={"url": url, "title": title},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Supabase insert failed for %s: %s", url, e)
        return False
 
 
# ---------------------------------------------------------------- Helpers
def is_quiet_hours():
    hour = datetime.now(AMSTERDAM_TZ).hour
    return hour >= QUIET_START or hour < QUIET_END
 
 
def wait_until_active():
    if not is_quiet_hours():
        return
    logging.info("Quiet hours active, sleeping until 06:00 Amsterdam time...")
    while is_quiet_hours():
        time.sleep(60)
    logging.info("Quiet hours over, resuming searches.")
 
 
def send_telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
        r.raise_for_status()
        logging.info("Telegram message sent")
        return True
    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False
 
 
def make_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    options.add_argument(f"user-agent={random.choice(user_agents)}")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
 
 
def parse_price(text):
    """'€ 850', '€ 1.250', '€850,00' -> int, or None if no price found."""
    m = re.search(r"€\s*([\d.,]+)", text or "")
    if not m:
        return None
    num = re.sub(r"[.,]\d{2}$", "", m.group(1))
    num = re.sub(r"[.,]", "", num)
    return int(num) if num.isdigit() else None
 
 
# ---------------------------------------------------------------- Main logic
def scrape_listings(driver):
    """Return [(url, title, price_text)] for every listing card on the page."""
    try:
        driver.get(SEARCH_URL)
        wait = random.uniform(7, 11)
        logging.info("Waiting %.1fs for page to render", wait)
        time.sleep(wait)
    except Exception as e:
        logging.error("Failed to load page: %s", e)
        return []
 
    soup = BeautifulSoup(driver.page_source, "html.parser")
    results, seen_on_page = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/en/for-rent/" not in href or "properties-" in href:
            continue
        url = "https://kamernet.nl" + href if href.startswith("/") else href
        if url in seen_on_page:
            continue
        seen_on_page.add(url)
 
        card_text = a.get_text(separator=" ", strip=True)
        parts = [p.strip() for p in card_text.split("·") if p.strip()]
        title = parts[1] if len(parts) > 1 else parts[0] if parts else "New listing"
        price_text = next((p for p in parts if "€" in p), "")
        results.append((url, title, price_text))
 
    logging.info("Found %d listing links", len(results))
    if not results:
        # Helps diagnose blocking (captcha / "access denied") or a changed page layout
        logging.warning("No listings found. Page title: %r, page size: %d chars",
                        driver.title, len(driver.page_source))
    return results
 
 
def run_cycle(driver):
    new_count = 0
    for url, title, price_text in scrape_listings(driver):
        if url in known_urls or url in skipped_urls:
            continue
 
        price = parse_price(price_text)
        if price is not None and price > MAX_RENT:
            skipped_urls.add(url)
            continue
 
        exists = listing_exists(url)
        if exists is None:          # Supabase unreachable: skip, retry next cycle (no duplicate risk)
            continue
        if exists:
            known_urls.add(url)
            continue
 
        if BASELINE:                # first-run mode: remember it, but don't notify
            if save_listing(url, title):
                known_urls.add(url)
                new_count += 1
            continue
 
        msg = f"🏠 {title}"
        if price_text:
            msg += f" — {price_text}"
        msg += f"\n{url}"
 
        # Save only after Telegram succeeded, so a failed send is retried next cycle
        if send_telegram(msg) and save_listing(url, title):
            known_urls.add(url)
            new_count += 1
 
    logging.info("%d new listings sent this cycle", new_count)
 
 
def human_sleep(base_seconds):
    total = base_seconds + random.uniform(30, 90)
    logging.info("Sleeping %.0fs", total)
    time.sleep(total)
 
 
if __name__ == "__main__":
    missing = [n for n, v in [("BOT_TOKEN", BOT_TOKEN), ("CHAT_ID", CHAT_ID),
                              ("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY)] if not v]
    if missing:
        sys.exit(f"Set these environment variables first: {', '.join(missing)}")
 
    # Scheduled (--once) runs skip quiet hours: nothing to do between 22:00 and 06:00 Amsterdam time
    if RUN_ONCE and not BASELINE and is_quiet_hours():
        logging.info("Quiet hours (22:00-06:00 Amsterdam time), skipping this run.")
        sys.exit(0)
 
    logging.info("Bot started...")
    driver = make_driver()
    try:
        if RUN_ONCE:
            run_cycle(driver)
        else:
            while True:
                wait_until_active()
                try:
                    run_cycle(driver)
                except Exception as e:
                    logging.error("Unexpected error: %s", e)
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = make_driver()
                human_sleep(250)
    finally:
        driver.quit()    
