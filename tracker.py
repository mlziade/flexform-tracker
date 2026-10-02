#!/usr/bin/env python3
"""Flexform price tracker.

Scrapes the product listing pages of https://www.flexform.com.br/ and stores a
price observation for every product in a SQLite database.

    python tracker.py              # run forever, one check every hour
    python tracker.py --once       # single check (use with Task Scheduler / cron)
    python tracker.py --interval 15
    python tracker.py --db other.db

Only the standard library is used.
"""
import argparse
import html
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://www.flexform.com.br"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) flexform-price-tracker/1.0"

# Only office chairs are tracked. Products are accepted if their category (taken
# from the URL / breadcrumb) is exactly this one; gaming and fixed chairs are not.
OFFICE_CATEGORY = "cadeiras/cadeiras-de-escritorio"
CATEGORIES = ["/" + OFFICE_CATEGORY]

# Sitemap paths that are never products.
NON_PRODUCT_PREFIXES = ("/institucional", "/guias", "/account", "/login", "/checkout", "/busca")

PAGE_SIZE = 100          # the site caps _limit at 100
MAX_PAGES = 20           # safety stop for pagination
REQUEST_DELAY = 0.5      # seconds each worker waits before a request, to be polite
WORKERS = 4              # parallel product-page fetches (~3 requests/s in total)
REQUEST_TIMEOUT = 90     # uncached top-level categories can take 30s+
RETRIES = 3

DEFAULT_DB = Path(__file__).parent / "data" / "flexform_prices.db"
DEFAULT_INTERVAL_MIN = 60

log = logging.getLogger("flexform")

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


# --------------------------------------------------------------------------- #
# Fetching / parsing
# --------------------------------------------------------------------------- #
def fetch(url):
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:      # 404 etc: retrying will not help
                raise
            last_err = e
            log.warning("fetch failed (%d/%d) %s: %s", attempt, RETRIES, url, e)
            time.sleep(2 * attempt)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            log.warning("fetch failed (%d/%d) %s: %s", attempt, RETRIES, url, e)
            time.sleep(2 * attempt)
    raise RuntimeError(f"giving up on {url}: {last_err}")


def parse_brl(text):
    """'R$ 7.569,00' / '799,00' -> 7569.0 / 799.0; None if no number."""
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})", text)
    if not m:
        return None
    return float(m.group(1).replace(".", "") + "." + m.group(2))


ARTICLE_RE = re.compile(r'<article class="produto"(.*?)</article>', re.S)
ATTR_RE = re.compile(r'data-gtm-(id|name|price)="([^"]*)"')
HREF_RE = re.compile(r'<a[^>]+href="(https://www\.flexform\.com\.br/[^"]+)"')
CASH_RE = re.compile(r'class="preco-boleto">\s*<strong>(.*?)</strong>', re.S)


def parse_listing(page_html):
    """Return a list of product dicts found on a listing page."""
    products = []
    for m in ARTICLE_RE.finditer(page_html):
        body = m.group(0)
        attrs = dict(ATTR_RE.findall(body.split(">", 1)[0]))
        href = HREF_RE.search(body)
        if "id" not in attrs or not href:
            continue
        cash = CASH_RE.search(body)
        url = href.group(1)
        # /cadeiras/cadeiras-de-escritorio/cadeira-x -> cadeiras/cadeiras-de-escritorio
        segments = url.removeprefix(BASE_URL + "/").split("?")[0].split("/")[:-1]
        products.append({
            "id": int(attrs["id"]),
            "name": html.unescape(attrs.get("name", "")).strip(),
            "url": url,
            "category": "/".join(segments) or None,
            "price_list": parse_brl(attrs.get("price")),
            "price_cash": parse_brl(cash.group(1)) if cash else None,
            "available": None,       # listing pages do not say
            "source": "listing",
        })
    return products


def scrape_category(path):
    """Page through one category listing, return all its products."""
    found = []
    for page in range(MAX_PAGES):
        url = f"{BASE_URL}{path}?_limit={PAGE_SIZE}&_offset={page * PAGE_SIZE}"
        batch = parse_listing(fetch(url))
        found.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(REQUEST_DELAY)
    for item in found:  # flat URLs like /cadeira-body-amarel carry no category
        item["category"] = item["category"] or path.strip("/")
    # The office-chair page also lists a few gaming chairs (/gaming/...); skip them.
    office = [i for i in found if i["category"] == OFFICE_CATEGORY]
    if len(office) < len(found):
        log.info("  skipped %d non-office-chair products (e.g. gaming)", len(found) - len(office))
    return office


# Product pages: fields are read from the schema.org Product JSON-LD block.
# (Regex rather than json.loads: the site's JSON-LD is occasionally malformed.)
JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
LD_SKU_RE = re.compile(r'"sku":\s*"(\d+)"')
LD_NAME_RE = re.compile(r'"name":\s*"((?:[^"\\]|\\.)*)"')
LD_PRICE_RE = re.compile(r'"price":\s*"([\d.]+)"')
LD_AVAIL_RE = re.compile(r'"availability":\s*"[^"]*/(\w+)"')
LD_CRUMB_RE = re.compile(r'"item":\s*"https://www\.flexform\.com\.br/([^"]*)"')
LIST_PRICE_RE = re.compile(r'class="ou">\s*ou\s*<span content="([^"]+)"')


def parse_product_page(page_html, url):
    """Return a product dict, or None if the page is not a product page."""
    block = next((b for b in JSONLD_RE.findall(page_html) if '"@type": "Product"' in b), None)
    if block is None:
        return None
    sku, price = LD_SKU_RE.search(block), LD_PRICE_RE.search(block)
    name = LD_NAME_RE.search(block)
    if not (sku and name):
        return None
    avail = LD_AVAIL_RE.search(block)
    list_price = LIST_PRICE_RE.search(page_html)
    crumbs = [b for b in (m for blk in JSONLD_RE.findall(page_html) if "BreadcrumbList" in blk
                          for m in LD_CRUMB_RE.findall(blk)) if b]
    category = "/".join(crumbs[-1].split("/")[:-1]) if crumbs else ""
    return {
        "id": int(sku.group(1)),
        "name": html.unescape(json.loads(f'"{name.group(1)}"')).strip(),
        "url": url,
        "category": category or None,
        "price_cash": float(price.group(1)) if price else None,
        "price_list": parse_brl(list_price.group(1)) if list_price else None,
        "available": None if not avail else int(avail.group(1) == "InStock"),
        "source": "product",
    }


def sitemap_urls():
    xml = fetch(f"{BASE_URL}/sitemap.xml")
    urls = set()
    for u in re.findall(r"<loc>(.*?)</loc>", xml):
        path = u.removeprefix(BASE_URL).split("?")[0].rstrip("/")
        if not path or path.startswith(NON_PRODUCT_PREFIXES):
            continue
        # office-chair pages, plus flat URLs (/cadeira-x) whose category is only
        # known once the page is read
        if path.startswith("/" + OFFICE_CATEGORY + "/") or path.count("/") == 1:
            urls.add(f"{BASE_URL}{path}")
    return urls


def scrape_listings():
    """Return ({id: product}, [error strings]) from the category listings."""
    products, errors = {}, []
    for i, path in enumerate(CATEGORIES):
        if i:
            time.sleep(REQUEST_DELAY)
        try:
            items = scrape_category(path)
            log.info("%-28s %3d products", path, len(items))
            for item in items:
                products.setdefault(item["id"], item)
        except Exception as e:  # keep going; other categories may still work
            log.error("category %s failed: %s", path, e)
            errors.append(f"{path}: {e}")
    return products, errors


def fetch_product(url):
    """Thread-pool worker: returns (url, product or None, error or None).
    Touches no shared state; each worker waits REQUEST_DELAY before its request."""
    time.sleep(REQUEST_DELAY)
    try:
        return url, parse_product_page(fetch(url), url), None
    except Exception as e:      # HTTPError included; the caller decides what 404 means
        return url, None, e


def scrape_unlisted(conn, listed, workers=WORKERS):
    """Fetch product pages the listings do not show (mostly out-of-stock or
    colour variants). Candidates: sitemap URLs + products we already know."""
    errors = []
    try:
        candidates = sitemap_urls()
    except Exception as e:
        log.error("sitemap failed: %s", e)
        errors.append(f"sitemap: {e}")
        candidates = set()
    candidates.update(r[0] for r in conn.execute("SELECT url FROM products"))
    skip = {r[0] for r in conn.execute("SELECT url FROM ignored_urls")}
    skip.update(p["url"].split("?")[0].rstrip("/") for p in listed.values())
    todo = sorted(candidates - skip)
    log.info("fetching %d product pages not covered by the listings", len(todo))

    # Pages are fetched in parallel (network-bound); results are then classified
    # one by one in URL order, so the outcome does not depend on thread timing.
    found, not_products = {}, []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo) or 1))) as pool:
        results = pool.map(fetch_product, todo)
        for n, (url, item, err) in enumerate(results, 1):
            if n % 25 == 0:
                log.info("  product pages: %d/%d", n, len(todo))
            if isinstance(err, urllib.error.HTTPError) and err.code == 404:
                not_products.append(url)     # gone from the site; keep history, stop polling
                continue
            if err is not None:
                log.error("product %s failed: %s", url, err)
                errors.append(f"{url}: {err}")
                continue
            if item is None or item["category"] != OFFICE_CATEGORY:
                not_products.append(url)      # not a product, or not an office chair
            elif item["id"] in listed or item["id"] in found:
                not_products.append(url)      # old slug of a product we already track
            else:
                found.setdefault(item["id"], item)
    return found, not_products, errors


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    return conn


def run_check(conn, changes_only=False, workers=WORKERS):
    """Scrape and store prices. Returns (products_found, errors).

    changes_only: write a price_history row only when a product's price or
    availability differs from its latest stored row, and leave the database file
    untouched when nothing changed (so it can be committed to git only on change).
    """
    started_at = now_iso()
    products, errors = scrape_listings()
    if products:   # without listings we cannot tell what is "unlisted"; skip the second pass
        extra, not_products, extra_errors = scrape_unlisted(conn, products, workers)
        for pid, p in extra.items():
            products.setdefault(pid, p)
        errors += extra_errors
    else:
        not_products = []
    checked_at = now_iso()

    rows = []   # price_history rows to write
    for p in products.values():
        if changes_only and not price_changed(conn, p):
            continue
        rows.append(p)

    new_ignored = [u for u in not_products
                   if not conn.execute("SELECT 1 FROM products WHERE url=?", (u,)).fetchone()
                   and not conn.execute("SELECT 1 FROM ignored_urls WHERE url=?", (u,)).fetchone()]
    if changes_only and not (rows or new_ignored):
        log.info("no changes in %d products; database untouched", len(products))
        return len(products), errors

    with conn:
        run_id = conn.execute(
            "INSERT INTO runs (started_at, finished_at, products_found, complete, errors)"
            " VALUES (?, ?, ?, ?, ?)",
            (started_at, checked_at, len(products), int(not errors and bool(products)),
             "\n".join(errors) or None),
        ).lastrowid
        for p in products.values():
            upsert_product(conn, p, checked_at, touch_last_seen=not changes_only)
        for p in rows:
            conn.execute(
                """INSERT INTO price_history
                       (run_id, product_id, checked_at, price_cash, price_list, available, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, p["id"], checked_at, p["price_cash"], p["price_list"],
                 p["available"], p["source"]),
            )
        conn.executemany(
            "INSERT OR IGNORE INTO ignored_urls (url, checked_at) VALUES (?, ?)",
            [(u, checked_at) for u in new_ignored],
        )
    log.info("run %d: %d products seen, %d price rows written%s", run_id, len(products),
             len(rows), f" ({len(errors)} errors)" if errors else "")
    return len(products), errors


def price_changed(conn, p):
    """True if the product is new or its price/availability differs from the latest row.
    availability=None (listing pages) means 'unknown' and never counts as a change."""
    last = conn.execute(
        "SELECT price_cash, price_list, available FROM price_history"
        " WHERE product_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    if last is None:
        return True
    if (p["price_cash"], p["price_list"]) != last[:2]:
        return True
    return p["available"] is not None and p["available"] != last[2]


def upsert_product(conn, p, seen_at, touch_last_seen):
    """Insert a product, or update its descriptive fields if they changed.
    last_seen is only bumped when touch_last_seen (it would otherwise dirty the db file)."""
    conn.execute(
        """INSERT INTO products (id, name, url, category, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, url=excluded.url, category=excluded.category,
               last_seen=CASE WHEN ? THEN excluded.last_seen ELSE products.last_seen END
           WHERE ? OR products.name IS NOT excluded.name
                   OR products.url IS NOT excluded.url
                   OR products.category IS NOT excluded.category""",
        (p["id"], p["name"], p["url"], p["category"], seen_at, seen_at,
         touch_last_seen, touch_last_seen),
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Track Flexform product prices in SQLite.")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_MIN,
                    help="minutes between checks (default: %(default)s)")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help="parallel product-page fetches (default: %(default)s)")
    ap.add_argument("--changes-only", action="store_true",
                    help="store a price row only when price/availability changed; leave the db "
                         "file untouched if nothing did (for committing the db to git)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = open_db(args.db)

    if args.once:
        count, _ = run_check(conn, args.changes_only, args.workers)
        return 0 if count else 1      # partial errors are logged, not fatal

    log.info("tracking every %g min -> %s (Ctrl+C to stop)", args.interval, args.db)
    try:
        while True:
            t0 = time.monotonic()
            try:
                run_check(conn, args.changes_only, args.workers)
            except Exception:
                log.exception("check failed; will retry next cycle")
            time.sleep(max(0, args.interval * 60 - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
