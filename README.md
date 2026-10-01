# Flexform price tracker

Tracks the price of every **office chair** (all colour variants) on
https://www.flexform.com.br/ and keeps the history in a SQLite database,
`data/flexform_prices.db`, committed to this repo.

It runs by itself on GitHub Actions every 30 minutes
([`.github/workflows/track.yml`](.github/workflows/track.yml)). Nothing to run locally.
Each run checks the site and, **only if a price or stock status changed**, commits the
updated database. So the commit history of `data/flexform_prices.db` is a log of price changes.

- Run it now: Actions tab -> *Track prices* -> *Run workflow*.
- GitHub's schedule is best-effort: runs are sometimes delayed by several minutes.
- Public repos have scheduled workflows paused after 60 days without repo activity;
  if that ever happens, re-enable it in the Actions tab.

## Viewing the history

Open [`index.html`](index.html) in a browser: a price chart (step lines, since a price holds
until it changes) with a timeframe selector (24h / 7d / 30d / 90d / 1y / All / custom dates),
cash or list price, R$ or % change, and a searchable list of every product with its
current price, change over the timeframe, stock and a link to the product page.
Tick up to 8 products to plot them; hover the chart for exact values.

- It reads `data/flexform_prices.db` with [sql.js](https://github.com/sql-js/sql.js) (loaded from
  cdnjs, so it needs an internet connection). Nothing is uploaded.
- Browsers don't let a page opened from disk (`file://`) read other files, so it will ask you to
  pick the database file (`data/flexform_prices.db`). Served over http it loads it automatically,
  e.g. `python -m http.server` in this folder, then http://localhost:8000.

## Using the data

Download `data/flexform_prices.db` (or clone the repo) and query it with any SQLite tool
(the table definitions are in [`schema.sql`](schema.sql)):

```sql
-- current prices
SELECT name, price_cash, price_list, available FROM latest_prices ORDER BY name;

-- price history of one product
SELECT checked_at, price_cash, price_list FROM price_history
WHERE product_id = 12688 ORDER BY checked_at;

-- every price change, with the previous price
SELECT p.name, h.checked_at, h.price_cash,
       LAG(h.price_cash) OVER (PARTITION BY h.product_id ORDER BY h.checked_at) AS previous
FROM price_history h JOIN products p ON p.id = h.product_id
ORDER BY h.checked_at DESC;
```

Because only changes are stored, a product's price at time *t* is its latest
`price_history` row at or before *t*.

| table | contents |
|---|---|
| `products` | one row per product: id, name, url, category, first seen |
| `price_history` | a row when a product is first seen and whenever its price or stock changes: `price_cash` (à vista, PIX/boleto), `price_list` (full price, up to 10x on card), `available` (1/0/NULL), `source` |
| `runs` | one row per run that changed something: time, products found, `complete` flag, errors |
| `ignored_urls` | sitemap URLs that are not office-chair products or are old slugs of a tracked product |
| `latest_prices` (view) | the most recent price of each product |

Prices are in BRL; `price_cash` is 10 % below `price_list`. `available` is NULL when the
price came from a listing page, which doesn't show stock; it never counts as a change.

## How it works

1. **Listing pass** - reads the office-chair listing
   (`/cadeiras/cadeiras-de-escritorio?_limit=100&_offset=N`). Each product card carries
   its id, name, list price and PIX/boleto price, so this is a couple of requests.
   Gaming chairs that Flexform also shows on that page are skipped.
2. **Product-page pass** - some products (mostly out of stock, plus some colour variants)
   are not shown in the listing. They are found through `sitemap.xml` and the products
   already in the database, and read from their own page (schema.org JSON-LD), which also
   gives stock availability. These pages are fetched in parallel (4 workers by default),
   which is what keeps a run to a few seconds.

Requests are spaced 0.5 s apart; `robots.txt` doesn't disallow any of these pages.

## Running locally (optional)

Python 3.9+, standard library only.

```
python tracker.py --once                 # one check, store every price
python tracker.py --once --changes-only  # what the workflow runs
python tracker.py --workers 8            # more parallel fetches (default 4, about 3 requests/s)
python tracker.py                        # loop forever, every 30 min (--interval N)
```

Without `--changes-only` a row is stored for every product on every run.

## Caveats

- It parses the site's HTML, so a redesign of the product cards or JSON-LD will break
  it; the workflow then fails (red run in the Actions tab) when no products are found.
- A new product is picked up when it appears in the listing or the sitemap.
