import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


# Use this for everything:
LISTING_URL = "https://woldringverhuur.nl/ons-aanbod/"

# Or use this if you ONLY want apartments:
# LISTING_URL = "https://woldringverhuur.nl/appartementen/"

CHECK_INTERVAL_SECONDS = 30 * 60
STATE_FILE = Path("woldring_state.json")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

ALERT_ON_FIRST_RUN = True
IGNORE_PARKING = True

HEADERS = {
    "User-Agent": "Mozilla/5.0 apartment-availability-monitor/1.0 "
                  "(personal use; checks every 30 minutes)"
}

NAV_TEXTS = {
    "Mijn Woldring",
    "Veelgestelde vragen",
    "English",
    "Ons aanbod",
    "Appartementen",
    "Studio's",
    "Studio’s",
    "Kamers",
    "Parkeerplaatsen",
    "Nieuws",
    "Over ons",
    "Over onze werkwijze",
    "Geschiedenis van Woldring Verhuur",
    "De Woldring Locatie",
    "Vacatures",
    "Totaal aanbod",
    "Hoe wij werken",
    "Geschiedenis",
    "Bekijk onze privacyverklaring",
}


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "known_active_ids": [],
        "last_seen": {},
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def fetch_html() -> str:
    response = requests.get(LISTING_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def is_property_anchor(a: Tag) -> bool:
    title = clean_text(a.get_text(" ", strip=True))
    href = a.get("href")

    if not title or not href:
        return False

    if title in NAV_TEXTS:
        return False

    if title.lower().startswith("klik hier"):
        return False

    full_url = urljoin(LISTING_URL, href)
    parsed = urlparse(full_url)

    if "woldringverhuur.nl" not in parsed.netloc:
        return False

    path = parsed.path.strip("/")

    # Most property pages are single-slug URLs like /oosterstraat-19a-14/
    # while category pages are excluded above.
    if "/" in path or not path:
        return False

    # Most real addresses contain at least one digit.
    if not re.search(r"\d", title):
        return False

    if IGNORE_PARKING and title.lower().startswith("parkeerplaats"):
        return False

    return True


def text_until_next_property_anchor(anchor: Tag, property_anchor_ids: set[int]) -> str:
    chunks = [clean_text(anchor.get_text(" ", strip=True))]

    for node in anchor.next_elements:
        if isinstance(node, Tag) and node.name == "a" and id(node) in property_anchor_ids and node is not anchor:
            break

        if isinstance(node, NavigableString):
            text = clean_text(str(node))
            if text:
                chunks.append(text)

    return clean_text(" ".join(chunks))


def parse_price(text: str) -> str | None:
    match = re.search(r"€\s?[\d\.,]+(?:\s*per maand)?", text)
    return match.group(0) if match else None


def parse_surface(text: str) -> str | None:
    match = re.search(r"Oppervlakte:\s*([\d\.,]+\s*m²)", text)
    return match.group(1) if match else None


def parse_availability(text: str) -> str:
    if "Per direct beschikbaar" in text:
        return "Per direct beschikbaar"
    if "Available immediately" in text:
        return "Available immediately"

    match = re.search(r"Beschikbaar per:\s*\d{2}-\d{2}-\d{4}", text)
    if match:
        return match.group(0)

    match = re.search(r"Available from:\s*\d{2}-\d{2}-\d{4}", text)
    if match:
        return match.group(0)

    if "Binnenkort beschikbaar" in text:
        return "Binnenkort beschikbaar"
    if "Niet beschikbaar" in text:
        return "Niet beschikbaar"
    if "Not available" in text:
        return "Not available"

    return "Onbekend"


def is_actionable_available(text: str) -> bool:
    # Woldring sometimes still includes "Inschrijving gesloten" in the listing text,
    # even when the card shows "Beschikbaar per". For our alerting purpose, we care
    # about anything newly marked as available.
    availableish = (
        "Per direct beschikbaar" in text
        or "Beschikbaar per:" in text
        or "Available from:" in text
        or "Binnenkort beschikbaar" in text
    )

    definitely_unavailable = (
        "Niet beschikbaar" in text
        or "Not available" in text
        or "Verhuurd" in text
        or "Rented" in text
    )

    return availableish and not definitely_unavailable


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    anchors = [a for a in soup.find_all("a") if is_property_anchor(a)]
    property_anchor_ids = {id(a) for a in anchors}

    listings = []

    for a in anchors:
        title = clean_text(a.get_text(" ", strip=True))
        url = urljoin(LISTING_URL, a.get("href"))
        text = text_until_next_property_anchor(a, property_anchor_ids)

        listing = {
            "id": urlparse(url).path.strip("/"),
            "title": title,
            "url": url,
            "available": is_actionable_available(text),
            "availability": parse_availability(text),
            "price": parse_price(text),
            "surface": parse_surface(text),
            "raw_text": text[:500],
        }

        listings.append(listing)

    # Remove duplicate URLs while preserving order
    unique = {}
    for listing in listings:
        unique.setdefault(listing["id"], listing)

    return list(unique.values())


def send_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook variable not set. Message would have been:")
        print(message)
        return

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": message,
            "username": "Woldring Apartment Watcher",
        },
        timeout=30,
    )
    response.raise_for_status()


def format_listing_message(listings: list[dict], first_run: bool = False) -> str:
    header = "Woldring monitor started. Currently available:" if first_run else "@everyone New Woldring listing available:"

    parts = [header]

    for item in listings:
        line = f"\n{item['title']}\n{item['availability']}"

        if item["price"]:
            line += f"\n{item['price']}"
        if item["surface"]:
            line += f"\n{item['surface']}"

        line += f"\n{item['url']}"
        parts.append(line)

    return "\n".join(parts)


def check_once() -> None:
    state = load_state()

    html = fetch_html()
    listings = parse_listings(html)
    # TEST BELOW
    if os.environ.get("WOLDRING_TEST_FAKE") == "1":
        listings.append({
            "id": "test-listing",
            "title": "TEST LISTING - ignore",
            "url": "https://woldringverhuur.nl/test/",
            "available": True,
            "availability": "Per direct beschikbaar",
            "price": "€ 999 per maand",
            "surface": "42 m²",
            "raw_text": "fake test listing",
        })
    # END TEST
    active = [item for item in listings if item["available"]]
    active_ids = {item["id"] for item in active}
    known_active_ids = set(state.get("known_active_ids", []))

    print("Currently actionable listings:")
    for item in active:
        print("-", item["title"], "|", item["availability"], "|", item["url"])

    first_run = not STATE_FILE.exists()

    if first_run:
        new_active = active if ALERT_ON_FIRST_RUN else []
    else:
        new_active = [item for item in active if item["id"] not in known_active_ids]

    if new_active:
        send_discord(format_listing_message(new_active, first_run=first_run))

    state["known_active_ids"] = sorted(active_ids)
    state["last_seen"] = {
        item["id"]: {
            "title": item["title"],
            "url": item["url"],
            "available": item["available"],
            "availability": item["availability"],
            "price": item["price"],
            "surface": item["surface"],
        }
        for item in listings
    }

    save_state(state)

    print(
        f"Checked {len(listings)} listings. "
        f"Actionable available: {len(active)}. "
        f"New alerts: {len(new_active)}."
    )


def main() -> None:
    print(f"Monitoring {LISTING_URL}")
    print(f"Checking every {CHECK_INTERVAL_SECONDS // 60} minutes.")

    while True:
        try:
            check_once()
        except Exception as exc:
            print(f"Error during check: {exc}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
