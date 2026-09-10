# -*- coding: utf-8 -*-
"""The shared database, behind HTTP.

Every read and write the agents do goes over the wire on purpose. State reached
through a local file or an in-process dict would be invisible to Orientim —
source 18, "filesystem state", is a declared limit — and this lab exists to
test what Orientim *can* see. Putting the database behind an API is also how a
real fleet is built, so nothing is contorted to make the point.

SQLite, seeded identically on every start, so the whole lab is reproducible.
"""
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9103
DB = os.environ.get("LAB_DB", ":memory:")

_lock = threading.Lock()
_conn = None

SEED = [
    # order_id, customer, status, total, country, card_last4, opened_days
    (4471, "ana",   "shipped",   129.00, "AL", "4242", 3),
    (4472, "bekim", "processing", 89.50, "AL", "4242", 1),
    (4473, "cara",  "lost",      450.00, "US", "9999", 21),
    (4474, "dritan", "delivered", 19.99, "XK", "1111", 40),
    (4475, "elira", "processing", 2400.00, "NG", "0000", 0),
]

ARTICLES = [
    (1, "refund policy", "Refunds within 30 days of delivery."),
    (2, "lost parcels", "A parcel is lost after 14 days without a scan."),
    (3, "high value", "Orders above 1000 need a manual risk review."),
]


def _db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB, check_same_thread=False)
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders(
                order_id INTEGER PRIMARY KEY, customer TEXT, status TEXT,
                total REAL, country TEXT, card_last4 TEXT, opened_days INTEGER);
            CREATE TABLE IF NOT EXISTS articles(
                id INTEGER PRIMARY KEY, title TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT);
        """)
        _conn.executemany("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?)",
                          SEED)
        _conn.executemany("INSERT OR REPLACE INTO articles VALUES (?,?,?)",
                          ARTICLES)
        _conn.commit()
    return _conn


def order(order_id):
    cur = _db().execute("SELECT order_id, customer, status, total, country, "
                        "card_last4, opened_days FROM orders WHERE order_id=?",
                        (order_id,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ("order_id", "customer", "status", "total", "country",
            "card_last4", "opened_days")
    return dict(zip(keys, row))


def search_articles(q):
    like = "%" + (q or "").lower() + "%"
    cur = _db().execute("SELECT id, title, body FROM articles "
                        "WHERE lower(title) LIKE ? OR lower(body) LIKE ? "
                        "ORDER BY id", (like, like))
    return [{"id": r[0], "title": r[1], "body": r[2]} for r in cur.fetchall()]


def record_event(kind, payload):
    with _lock:
        _db().execute("INSERT INTO events(kind, payload) VALUES (?,?)",
                      (kind, json.dumps(payload, sort_keys=True)))
        _db().commit()
    return {"recorded": kind}


def events():
    cur = _db().execute("SELECT kind, payload FROM events ORDER BY id")
    return [{"kind": k, "payload": json.loads(p)} for k, p in cur.fetchall()]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/db/events":
            return self._send(200, {"events": events()})
        if p == "/db/health":
            return self._send(200, {"ok": True, "orders": len(SEED)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        p = self.path.split("?")[0]
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})

        if p == "/db/order":
            row = order(int(req.get("order_id", 0)))
            return self._send(200 if row else 404,
                              row or {"error": "no such order"})
        if p == "/db/articles":
            return self._send(200, {"articles": search_articles(req.get("q"))})
        if p == "/db/event":
            return self._send(200, record_event(req.get("kind", "note"),
                                                req.get("payload") or {}))
        if p == "/db/reset":
            global _conn
            with _lock:
                _conn = None
            _db()
            return self._send(200, {"reset": True})
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    _db()
    print("state on %d" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
