"""Shared test doubles for storefront-api: an in-memory Postgres and a stub
payment client. Imported by the storefront and Assignment 2 test suites.

Importing this module stubs psycopg2, puts the storefront package on sys.path
and patches app.db / app.clients, so it must be imported before the app.

The fake understands exactly the SQL the app issues (anything else fails the
test loudly) and keeps a per-transaction undo journal, so a rolled-back
checkout really leaves no order behind - the DB-derived gauges depend on it.
"""
import contextlib
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "storefront-api"))

# --- stub psycopg2 before app.db imports it -------------------------------
fake_psycopg2 = types.ModuleType("psycopg2")
fake_extras = types.ModuleType("psycopg2.extras")
fake_pool = types.ModuleType("psycopg2.pool")
fake_extras.RealDictCursor = object


class _Pool:
    def __init__(self, *a, **k): pass


class IntegrityError(Exception):
    """Stands in for psycopg2.IntegrityError (unique violation)."""


fake_pool.ThreadedConnectionPool = _Pool
fake_psycopg2.extras = fake_extras
fake_psycopg2.pool = fake_pool
fake_psycopg2.IntegrityError = IntegrityError
sys.modules["psycopg2"] = fake_psycopg2
sys.modules["psycopg2.extras"] = fake_extras
sys.modules["psycopg2.pool"] = fake_pool

from app import clients, db  # noqa: E402

real_db_cursor = db.cursor  # kept to test the pool-wait logic itself

# --- in-memory database ----------------------------------------------------
PRODUCTS = {
    "KB-01": {"sku": "KB-01", "name": "Mechanical Keyboard", "price_cents": 4900, "stock": 40},
    "MS-01": {"sku": "MS-01", "name": "Wireless Mouse", "price_cents": 1900, "stock": 60},
    "OUT-01": {"sku": "OUT-01", "name": "Sold Out Thing", "price_cents": 500, "stock": 0},
}
ORDERS = {}
NEXT_ID = [1000]
COMMITS = []
PRODUCT_LOCKS = []  # every statement that row-locks products
SQL_LOG = []        # every statement, normalised, for assertions


def _order_view(oid, o):
    return {"id": oid, "status": o["status"], "total_cents": o["total_cents"],
            "item_count": o["item_count"], "customer": o["customer"],
            "created_at": "2026-10-25T00:00:00Z"}


class FakeConn:
    def __init__(self, cursor): self._cursor = cursor

    def commit(self):
        self._cursor.journal.clear()
        COMMITS.append("commit")

    def rollback(self):
        self._cursor.undo()
        COMMITS.append("rollback")


class FakeCursor:
    def __init__(self):
        self._rows = []
        self.journal = []  # undo actions for this transaction
        self.connection = FakeConn(self)

    def undo(self):
        while self.journal:
            self.journal.pop()()

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        SQL_LOG.append(s)
        self._rows = []
        if "FROM products" in s and "FOR UPDATE" in s:
            PRODUCT_LOCKS.append(s)
        if "FROM products WHERE sku = ANY" in s:
            self._rows = [dict(PRODUCTS[k]) for k in params[0] if k in PRODUCTS]
        elif s.startswith("SELECT sku, stock FROM products"):
            self._rows = [{"sku": p["sku"], "stock": p["stock"]} for p in PRODUCTS.values()]
        elif s.startswith("SELECT sku, name, price_cents, stock FROM products ORDER BY"):
            self._rows = [dict(p) for p in PRODUCTS.values()]
        elif "COUNT(*) AS n FROM orders" in s:
            self._rows = [{"n": sum(1 for o in ORDERS.values() if o["status"] == "PLACED")}]
        elif "FROM orders WHERE idempotency_key = %s" in s:
            self._rows = [{"id": k, "status": o["status"], "total_cents": o["total_cents"],
                           "item_count": o["item_count"], "request_hash": o["request_hash"]}
                          for k, o in ORDERS.items()
                          if o["idempotency_key"] is not None
                          and o["idempotency_key"] == params[0]]
        elif s.startswith("INSERT INTO orders"):
            total, count, customer, key, request_hash = params
            if key is not None and any(o["idempotency_key"] == key for o in ORDERS.values()):
                raise IntegrityError("duplicate key value violates unique constraint "
                                     "\"orders_idempotency_key_idx\"")
            NEXT_ID[0] += 1
            oid = NEXT_ID[0]
            ORDERS[oid] = {"status": "PLACED", "total_cents": total, "item_count": count,
                           "customer": customer, "idempotency_key": key,
                           "request_hash": request_hash}
            self.journal.append(lambda: ORDERS.pop(oid, None))
            self._rows = [{"id": oid}]
        elif s.startswith("INSERT INTO order_items"):
            pass
        elif s.startswith("UPDATE products SET stock = stock -"):
            qty, sku = params
            PRODUCTS[sku]["stock"] -= qty
            self.journal.append(lambda: PRODUCTS[sku].__setitem__(
                "stock", PRODUCTS[sku]["stock"] + qty))
        elif s.startswith("UPDATE products SET stock = %s"):
            for p in PRODUCTS.values():
                p["stock"] = params[0]
        elif "SELECT status, total_cents FROM orders WHERE id" in s:
            o = ORDERS.get(params[0])
            self._rows = [{"status": o["status"], "total_cents": o["total_cents"]}] if o else []
        elif "SELECT status FROM orders WHERE id" in s:
            o = ORDERS.get(params[0])
            self._rows = [{"status": o["status"]}] if o else []
        elif s.startswith("UPDATE orders SET status = 'CANCELLED'"):
            ORDERS[params[0]]["status"] = "CANCELLED"
        elif s.startswith("UPDATE orders SET status = 'FULFILLED'"):
            ORDERS[params[0]]["status"] = "FULFILLED"
        elif s.startswith("UPDATE products p SET stock"):
            pass
        elif s.startswith("SELECT id, status, total_cents") and "WHERE id = %s" in s:
            o = ORDERS.get(params[0])
            self._rows = [_order_view(params[0], o)] if o else []
        elif s.startswith("SELECT id, status, total_cents"):
            self._rows = [_order_view(k, v) for k, v in sorted(ORDERS.items(), reverse=True)]
        elif s.startswith("SELECT sku FROM products ORDER BY sku FOR UPDATE"):
            self._rows = [{"sku": k} for k in sorted(PRODUCTS)]
        elif s.startswith("SELECT p.sku FROM products p JOIN order_items"):
            pass
        elif "SELECT 1 AS ok" in s:
            self._rows = [{"ok": 1}]
        elif s.startswith(("ALTER TABLE orders", "CREATE UNIQUE INDEX")):
            pass
        else:
            raise AssertionError("unhandled SQL: " + s)

    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return self._rows


@contextlib.contextmanager
def fake_cursor(operation, commit=False):
    """Same contract as app.db.cursor: commit or roll back on exit."""
    cur = FakeCursor()
    try:
        yield cur
    except Exception:
        cur.connection.rollback()
        raise
    if commit:
        cur.connection.commit()
    else:
        cur.connection.rollback()  # a no-op after an explicit commit, as in psycopg2


db.cursor = fake_cursor
db.init_pool = lambda *a, **k: None
db.ping = lambda: True
db.close_pool = lambda: None

# --- stub payment client -----------------------------------------------------
PAYMENT_MODE = {"outcome": clients.PaymentOutcome.APPROVED}
CHARGES = []  # (order_id, request_id) of every charge attempt


def fake_charge(order_id, amount_cents, method, request_id):
    CHARGES.append((order_id, request_id))
    return PAYMENT_MODE["outcome"], {"status_code": 200, "duration_ms": 12.3}


clients.charge = fake_charge
clients.post_chaos = lambda path, payload: {"stubbed": path}
clients.get_chaos_status = lambda: {"stubbed": True}

# --- reporting -----------------------------------------------------------------
fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


def finish():
    print()
    print("FAILURES:", fails if fails else "none")
    sys.exit(1 if fails else 0)


def metric_value(text, needle):
    for line in text.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[1])
    return None
