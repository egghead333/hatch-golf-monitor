"""Hatch Golf New Releases monitor; standard library only.

First run of this version initializes release_seen_ids without alerts.
Subsequent runs alert on ANY product newly appearing in New Releases,
regardless of product category. Existing seen_ids are preserved.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE = "https://www.hatchgolf.com"
STATE = Path("hatch-products-seen.json")
RELEASES = "/collections/new-releases/products.json"
CATALOG = "/products.json"
PAGE_SIZE = 250
MAX_PAGES = 30


def get_json(url, attempts=3):
    for attempt in range(attempts):
        try:
            request = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; HatchReleaseMonitor/2.0)",
                "Accept": "application/json",
            })
            with urlopen(request, timeout=25) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f"Cannot retrieve {url}: {exc}") from exc
            time.sleep(2 * (attempt + 1))


def load_feed(path):
    found = {}
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"limit": PAGE_SIZE, "page": page})
        payload = get_json(f"{BASE}{path}?{query}")
        batch = payload.get("products")
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected product feed response: {path} page {page}")
        for product in batch:
            if product.get("id") is not None:
                found[str(product["id"])] = product
        if len(batch) < PAGE_SIZE:
            return found
    raise RuntimeError(f"Feed exceeds {MAX_PAGES} pages: {path}; refusing partial snapshot")


def category(product):
    title = str(product.get("title") or "").lower()
    ptype = str(product.get("product_type") or "").lower()
    name = f"{title} {ptype}"
    categories = [
        ("Driver", ("driver",)),
        ("Fairway", ("fairway", "3 wood", "3-wood")),
        ("Hybrid", ("hybrid",)),
        ("Mallet", ("mallet",)),
        ("Blade", ("blade",)),
        ("Putter", ("putter",)),
        ("Hoodie", ("hoodie", "hooded sweatshirt")),
        ("Hat", ("hat", "cap", "snapback", "headwear")),
        ("Ball marker", ("ball marker", "ballmarker", "marker")),
        ("Headcover", ("headcover", "head cover")),
        ("Apparel", ("shirt", "tee", "polo", "jacket", "sweatshirt")),
    ]
    for label, words in categories:
        if any(word in name for word in words):
            return label
    return ptype.title() if ptype else "New release"


def price(product):
    amounts = []
    for variant in product.get("variants") or []:
        try:
            amounts.append(float(variant["price"]))
        except (KeyError, ValueError, TypeError):
            continue
    if not amounts:
        return "Price unavailable"
    low, high = min(amounts), max(amounts)
    return f"${low:,.2f}" if low == high else f"${low:,.2f}–${high:,.2f}"


def availability(product):
    variants = product.get("variants") or []
    if not variants:
        return "Availability unknown"
    return "Available" if any(v.get("available") for v in variants) else "Sold out"


def notify(product):
    token = os.environ.get("PUSHOVER_API_TOKEN")
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not token or not user:
        raise RuntimeError("Missing PUSHOVER_API_TOKEN or PUSHOVER_USER_KEY secret")
    title = str(product.get("title") or "New Hatch Golf release")
    handle = product.get("handle")
    link = f"{BASE}/products/{handle}" if handle else f"{BASE}/collections/new-releases"
    message = f"{title}\n{category(product)} | {price(product)} | {availability(product)}\n{link}"
    data = urlencode({
        "token": token, "user": user,
        "title": "NEW HATCH GOLF DROP", "message": message,
        "url": link, "url_title": "View product",
        "priority": 2, "retry": 30, "expire": 120, "sound": "siren",
    }).encode()
    request = Request("https://api.pushover.net/1/messages.json", data=data, method="POST")
    try:
        with urlopen(request, timeout=25) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Pushover failed for product {product.get('id')}: {exc}") from exc
    if result.get("status") != 1:
        raise RuntimeError(f"Pushover rejected alert for product {product.get('id')}: {result}")
    print(f"Alert sent: {title}")


def save_state(state):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(STATE)


def main():
    local = datetime.now(ZoneInfo("America/Chicago"))
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and local.hour < 12:
        print("Outside 12 PM–12 AM Central monitoring window; skipping")
        return

    # The collection is the alert source. The full catalog is kept as an
    # additional inventory reference, not a source of alerts by itself.
    releases = load_feed(RELEASES)
    print(f"New Releases: {len(releases)} products")
    if not releases:
        raise RuntimeError("New Releases returned zero products; refusing to reset baseline")
    catalog = load_feed(CATALOG)
    print(f"Full catalog: {len(catalog)} products")
    if not catalog:
        raise RuntimeError("Full catalog returned zero products; refusing to reset baseline")

    all_ids = set(catalog) | set(releases)
    release_ids = set(releases)
    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise RuntimeError("Invalid inventory state; refusing to overwrite")
    else:
        state = {}

    # Migration from the earlier headcover-only monitor: preserve the
    # historical IDs, but baseline collection membership without alert spam.
    if "release_seen_ids" not in state:
        state["seen_ids"] = sorted(set(map(str, state.get("seen_ids", []))) | all_ids)
        state["release_seen_ids"] = sorted(release_ids)
        save_state(state)
        print(f"New Releases baseline initialized: {len(release_ids)} products; no alerts")
        return

    seen_releases = set(map(str, state["release_seen_ids"]))
    seen_catalog = set(map(str, state.get("seen_ids", [])))
    new = [p for pid, p in releases.items() if pid not in seen_releases]
    print(f"New listings in New Releases: {len(new)}")

    # Prioritize club covers, but alert for EVERY new collection listing.
    priority = {"Fairway": 0, "Hybrid": 1, "Mallet": 2, "Driver": 3, "Blade": 4}
    new.sort(key=lambda p: (priority.get(category(p), 5), str(p.get("title", ""))))
    for product in new:
        notify(product)  # Failure prevents marking this product as notified.
        seen_releases.add(str(product["id"]))
        state["release_seen_ids"] = sorted(seen_releases)
        state["seen_ids"] = sorted(seen_catalog | all_ids)
        save_state(state)

    state["release_seen_ids"] = sorted(seen_releases)
    state["seen_ids"] = sorted(seen_catalog | all_ids)
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
