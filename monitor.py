import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE = 'https://www.hatchgolf.com'
STATE = Path('hatch-products-seen.json')
FEEDS = ['/products.json', '/collections/new-releases/products.json']
COVER = re.compile(r'head\s*cover|headcover|driver cover|fairway cover|hybrid cover|mallet cover|blade cover|putter cover|wood cover', re.I)

def get_json(url, attempts=3):
    for attempt in range(attempts):
        try:
            req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; HatchReleaseMonitor/1.0)', 'Accept': 'application/json'})
            with urlopen(req, timeout=25) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f'Could not fetch {url}: {exc}') from exc
            time.sleep(2 * (attempt + 1))

def load_feed(path):
    products = {}
    for page in range(1, 21):
        url = f'{BASE}{path}?{urlencode({"limit": 250, "page": page})}'
        payload = get_json(url)
        batch = payload.get('products')
        if not isinstance(batch, list):
            raise RuntimeError(f'Unexpected response from {url}')
        for p in batch:
            if 'id' in p:
                products[str(p['id'])] = p
        if len(batch) < 250:
            return products
    raise RuntimeError(f'Feed pagination limit reached for {path}; refusing partial snapshot')

def is_cover(product):
    return bool(COVER.search(' '.join(str(product.get(k) or '') for k in ('title', 'product_type', 'tags'))))

def club_type(product):
    title = str(product.get('title', '')).lower()
    for label, terms in [('Mallet', ['mallet']), ('Hybrid', ['hybrid']), ('Fairway', ['fairway', '3 wood', '3-wood']), ('Driver', ['driver']), ('Blade putter', ['blade']), ('Putter', ['putter']), ('Wood', ['wood'])]:
        if any(term in title for term in terms):
            return label
    return 'Headcover / set'

def product_price(product):
    variants = product.get('variants') or []
    prices = sorted({float(v['price']) for v in variants if v.get('price') is not None})
    if not prices:
        return 'Price unavailable'
    return f'${prices[0]:,.2f}' + (f'–${prices[-1]:,.2f}' if len(prices) > 1 else '')

def availability(product):
    variants = product.get('variants') or []
    if not variants:
        return 'Availability unknown'
    return 'Available' if any(v.get('available') for v in variants) else 'Sold out'

def notify(product):
    token = os.environ.get('PUSHOVER_API_TOKEN')
    user = os.environ.get('PUSHOVER_USER_KEY')
    if not token or not user:
        raise RuntimeError('Pushover secrets missing')
    title = str(product.get('title') or 'New Hatch Golf release')
    handle = product.get('handle')
    link = f'{BASE}/products/{handle}' if handle else BASE + '/collections/new-releases'
    details = f'{club_type(product)} | {product_price(product)} | {availability(product)}'
    data = urlencode({'token': token, 'user': user, 'title': 'NEW HATCH GOLF DROP', 'message': f'{title}\n{details}\n{link}', 'url': link, 'url_title': 'View product', 'priority': 2, 'retry': 30, 'expire': 120, 'sound': 'siren'}).encode()
    req = Request('https://api.pushover.net/1/messages.json', data=data, method='POST')
    try:
        with urlopen(req, timeout=25) as response:
            result = json.load(response)
        if result.get('status') != 1:
            raise RuntimeError(f'Pushover rejected notification: {result}')
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f'Pushover notification failed for product {product.get("id")}: {exc}') from exc
    print(f'Alert sent: {title}')

def main():
    now = datetime.now(ZoneInfo('America/Chicago'))
    if os.environ.get('GITHUB_EVENT_NAME') == 'schedule' and not (12 <= now.hour < 21 or (now.hour == 21 and now.minute <= 30)):
        print('Outside 12:00–21:30 Central monitoring window; skipping')
        return
    products = {}
    for feed in FEEDS:
        batch = load_feed(feed)
        print(f'{feed}: {len(batch)} products')
        products.update(batch)
    if not products:
        raise RuntimeError('Empty catalog; refusing to overwrite inventory')
    current_ids = set(products)
    if not STATE.exists():
        STATE.write_text(json.dumps({'seen_ids': sorted(current_ids)}, indent=2) + '\n')
        print(f'Baseline initialized with {len(current_ids)} products; no alerts')
        return
    old = json.loads(STATE.read_text())
    seen = set(str(x) for x in old.get('seen_ids', []))
    new = [p for pid, p in products.items() if pid not in seen and is_cover(p)]
    print(f'{len(new)} new headcover listing(s) found')
    # Send each notification before recording the ID. If a send fails, retry next run.
    for p in sorted(new, key=lambda x: (club_type(x) not in ('Fairway', 'Hybrid', 'Mallet'), str(x.get('title')))):
        notify(p)
        seen.add(str(p['id']))
        STATE.write_text(json.dumps({'seen_ids': sorted(seen | (current_ids - {str(q['id']) for q in new}))}, indent=2) + '\n')
    seen.update(current_ids)
    STATE.write_text(json.dumps({'seen_ids': sorted(seen)}, indent=2) + '\n')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
