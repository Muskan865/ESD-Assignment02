-- Tiny Shop schema. Runs once, on an empty Postgres data directory.

CREATE TABLE IF NOT EXISTS products (
    sku         TEXT PRIMARY KEY,
    name        TEXT    NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    stock       INTEGER NOT NULL CHECK (stock >= 0)
);

-- Order lifecycle: PLACED -> FULFILLED, or PLACED -> CANCELLED.
-- "awaiting fulfilment" (the Gauge in Part B) is exactly COUNT(*) WHERE status='PLACED'.
CREATE TABLE IF NOT EXISTS orders (
    id           SERIAL PRIMARY KEY,
    status       TEXT        NOT NULL CHECK (status IN ('PLACED', 'FULFILLED', 'CANCELLED')),
    total_cents  INTEGER     NOT NULL,
    item_count   INTEGER     NOT NULL,
    customer     TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku         TEXT    NOT NULL REFERENCES products(sku),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    price_cents INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status);

INSERT INTO products (sku, name, price_cents, stock) VALUES
    ('KB-01', 'Mechanical Keyboard', 4900, 40),
    ('MS-01', 'Wireless Mouse',      1900, 60),
    ('HP-01', 'Studio Headphones',   8900, 25),
    ('MN-01', '27" Monitor',        21900, 12),
    ('CB-01', 'USB-C Cable',           900, 90)
ON CONFLICT (sku) DO NOTHING;

-- Assignment 2: idempotent order creation. A retried POST /api/orders (or gRPC
-- PlaceOrder) carrying the same key finds this row instead of placing a second
-- order. NULLs do not collide in a unique index, so keyless orders are fine.
-- The storefront also applies these on start-up (app/db.py), so a database
-- created by the Assignment 1 schema is upgraded without `down -v`.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS request_hash TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS orders_idempotency_key_idx ON orders (idempotency_key);
