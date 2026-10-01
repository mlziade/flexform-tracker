CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,   -- Flexform item id
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    category    TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    products_found  INTEGER,
    complete        INTEGER,           -- 1 if every category was fetched OK
    errors          TEXT
);

-- One row per product per run. Prices are in BRL.
--   price_cash : "a vista" price (PIX / boleto)
--   price_list : full price, paid in up to 10x on the card
--   available  : 1 in stock / 0 out of stock / NULL unknown (listing pages do not say)
--   source     : 'listing' (category page) or 'product' (the product's own page)
-- NULL prices mean the page showed none.
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    checked_at  TEXT NOT NULL,
    price_cash  REAL,
    price_list  REAL,
    available   INTEGER,
    source      TEXT
);

-- Sitemap URLs that are not office-chair product pages; never fetched again.
CREATE TABLE IF NOT EXISTS ignored_urls (
    url         TEXT PRIMARY KEY,
    checked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_product
    ON price_history(product_id, checked_at);

CREATE VIEW IF NOT EXISTS latest_prices AS
SELECT p.id, p.name, p.category, p.url, h.checked_at, h.price_cash, h.price_list, h.available
FROM products p
JOIN price_history h ON h.id = (
    SELECT id FROM price_history WHERE product_id = p.id ORDER BY id DESC LIMIT 1
);
