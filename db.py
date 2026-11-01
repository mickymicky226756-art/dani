"""PostgreSQL storage layer (works with Neon, including its pooled endpoint).

Ticket numbers are 1..total for each car.
A row in `tickets` exists only while a number is NOT available:
  reserved -> held for a user until expires_at (unpaid)
  pending  -> payment screenshot sent, waiting for admin
  sold     -> payment confirmed

All functions here are synchronous (blocking). bot.py calls them through a
thread wrapper so the Telegram event loop is never blocked by network latency.
"""
import random
import time

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool = None  # type: ignore

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users(
        id BIGINT PRIMARY KEY,
        name TEXT, username TEXT,
        lang TEXT NOT NULL DEFAULT 'en',
        phone TEXT,
        awaiting_phone INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cars(
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        price BIGINT NOT NULL,
        total BIGINT NOT NULL,
        photo TEXT,
        deadline BIGINT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        drawn INTEGER NOT NULL DEFAULT 0,
        winner_number BIGINT,
        winner_user BIGINT
    )""",
    "ALTER TABLE cars ADD COLUMN IF NOT EXISTS photo_hash TEXT",
    """CREATE TABLE IF NOT EXISTS payments(
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        car_id BIGINT NOT NULL,
        amount BIGINT NOT NULL,
        file_id TEXT NOT NULL,
        file_kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        decided_by BIGINT,
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tickets(
        car_id BIGINT NOT NULL,
        number BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        status TEXT NOT NULL,
        expires_at BIGINT,
        payment_id BIGINT,
        PRIMARY KEY(car_id, number)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_payment ON tickets(payment_id)",
    """CREATE TABLE IF NOT EXISTS admin_msgs(
        payment_id BIGINT NOT NULL,
        chat_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        PRIMARY KEY(payment_id, chat_id)
    )""",
]

# a ticket row counts as taken unless it is an expired reservation (needs 1 param: now)
LIVE = "(status<>'reserved' OR expires_at>%s)"


def init(url: str) -> None:
    global _pool
    _pool = ConnectionPool(
        url,
        min_size=1,
        max_size=8,
        # prepare_threshold=None: required for pgbouncer-style poolers (Neon "-pooler" host)
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
        check=ConnectionPool.check_connection,  # drop dead/idle-closed connections
        timeout=60,
        open=False,
    )
    _pool.open(wait=True, timeout=60)
    with _pool.connection() as c:
        for stmt in SCHEMA:
            c.execute(stmt)


def close() -> None:
    if _pool:
        _pool.close()


def _now() -> int:
    return int(time.time())


def _all(sql, p=()):
    with _pool.connection() as c:
        return c.execute(sql, p).fetchall()


def _one(sql, p=()):
    with _pool.connection() as c:
        return c.execute(sql, p).fetchone()


def _run(sql, p=()) -> int:
    with _pool.connection() as c:
        return c.execute(sql, p).rowcount


# ------------------------------------------------------------------ users
def upsert_user(uid, name, username):
    """Create/update the user and return the full row in a single round trip."""
    return _one(
        "INSERT INTO users(id,name,username,created_at) VALUES(%s,%s,%s,%s) "
        "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name, username=EXCLUDED.username "
        "RETURNING *",
        (uid, name, username, _now()),
    )


def get_user(uid):
    return _one("SELECT * FROM users WHERE id=%s", (uid,))


def set_lang(uid, lang):
    _run("UPDATE users SET lang=%s WHERE id=%s", (lang, uid))


def set_phone(uid, phone):
    _run("UPDATE users SET phone=%s, awaiting_phone=0 WHERE id=%s", (phone, uid))


def set_awaiting_phone(uid, flag):
    _run("UPDATE users SET awaiting_phone=%s WHERE id=%s", (int(flag), uid))


def all_user_ids():
    return [r["id"] for r in _all("SELECT id FROM users")]


# ------------------------------------------------------------------- cars
def add_car(name, price, total, photo, deadline) -> int:
    # a picture given at creation time was chosen by an admin ("manual"): the photos/ folder never overrides it
    return _one(
        "INSERT INTO cars(name,price,total,photo,photo_hash,deadline) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
        (name, price, total, photo, "manual" if photo else None, deadline),
    )["id"]


def set_car_photo(car_id, file_id, photo_hash):
    """photo_hash: sha1 of the local file that was uploaded, or 'manual' when an admin sent it."""
    _run("UPDATE cars SET photo=%s, photo_hash=%s WHERE id=%s", (file_id, photo_hash, car_id))


def count_cars() -> int:
    return _one("SELECT COUNT(*) AS c FROM cars")["c"]


def get_car(car_id):
    return _one("SELECT * FROM cars WHERE id=%s", (car_id,))


def active_cars():
    return _all("SELECT * FROM cars WHERE active=1 AND drawn=0 ORDER BY id")


def all_cars():
    return _all("SELECT * FROM cars ORDER BY id")


_CAR_FIELDS = {"name", "price", "total", "photo", "deadline", "active"}


def set_car_field(car_id, field, value):
    if field not in _CAR_FIELDS:
        raise ValueError(field)
    _run(f"UPDATE cars SET {field}=%s WHERE id=%s", (value, car_id))


def max_ticket_number(car_id) -> int:
    return _one("SELECT COALESCE(MAX(number),0) AS m FROM tickets WHERE car_id=%s", (car_id,))["m"]


# ---------------------------------------------------------------- tickets
def taken_count(car_id) -> int:
    return _one(
        f"SELECT COUNT(*) AS c FROM tickets WHERE car_id=%s AND {LIVE}", (car_id, _now())
    )["c"]


def taken_numbers(car_id, lo, hi) -> set:
    rows = _all(
        f"SELECT number FROM tickets WHERE car_id=%s AND number BETWEEN %s AND %s AND {LIVE}",
        (car_id, lo, hi, _now()),
    )
    return {r["number"] for r in rows}


def bucket_taken(car_id, size) -> dict:
    rows = _all(
        f"SELECT (number / %s::bigint) AS b, COUNT(*) AS c FROM tickets "
        f"WHERE car_id=%s AND {LIVE} GROUP BY 1",
        (size, car_id, _now()),
    )
    return {int(r["b"]): r["c"] for r in rows}


def reserve(car_id, number, user_id, minutes, max_per_user):
    """Returns 'ok' | 'taken' | 'limit' | 'closed' | 'invalid'."""
    now = _now()
    car = get_car(car_id)
    if not car or not car["active"] or car["drawn"] or car["deadline"] <= now:
        return "closed"
    if not (1 <= number <= car["total"]):
        return "invalid"
    with _pool.connection() as c:  # one transaction
        # serialise picks of the same user so the per-car limit cannot be bypassed
        c.execute("SELECT 1 FROM users WHERE id=%s FOR UPDATE", (user_id,))
        c.execute(
            "DELETE FROM tickets WHERE car_id=%s AND number=%s AND status='reserved' AND expires_at<=%s",
            (car_id, number, now),
        )
        if max_per_user:
            n = c.execute(
                f"SELECT COUNT(*) AS c FROM tickets WHERE car_id=%s AND user_id=%s AND {LIVE}",
                (car_id, user_id, now),
            ).fetchone()["c"]
            if n >= max_per_user:
                return "limit"
        cur = c.execute(
            "INSERT INTO tickets(car_id,number,user_id,status,expires_at) VALUES(%s,%s,%s,'reserved',%s) "
            "ON CONFLICT (car_id, number) DO NOTHING",
            (car_id, number, user_id, now + minutes * 60),
        )
        if cur.rowcount == 0:
            return "taken"
        # every new pick refreshes the hold on all of the user's unpaid numbers
        c.execute(
            "UPDATE tickets SET expires_at=%s WHERE user_id=%s AND status='reserved'",
            (now + minutes * 60, user_id),
        )
    return "ok"


def user_reserved(user_id, car_id=None):
    sql = "SELECT car_id, number, expires_at FROM tickets WHERE user_id=%s AND status='reserved' AND expires_at>%s"
    params = [user_id, _now()]
    if car_id is not None:
        sql += " AND car_id=%s"
        params.append(car_id)
    sql += " ORDER BY car_id, number"
    return _all(sql, params)


def release_number(user_id, car_id, number) -> int:
    """Remove ONE of the user's own unpaid (reserved) numbers. Never touches other users' tickets."""
    return _run(
        "DELETE FROM tickets WHERE car_id=%s AND number=%s AND user_id=%s AND status='reserved'",
        (car_id, number, user_id),
    )


def release_user_reserved(user_id) -> int:
    return _run("DELETE FROM tickets WHERE user_id=%s AND status='reserved'", (user_id,))


def user_tickets(user_id):
    return _all(
        "SELECT t.car_id, c.name AS car_name, t.number, t.status, t.expires_at "
        "FROM tickets t JOIN cars c ON c.id=t.car_id "
        "WHERE t.user_id=%s AND (t.status<>'reserved' OR t.expires_at>%s) "
        "ORDER BY t.car_id, t.number",
        (user_id, _now()),
    )


def sweep_expired():
    """Delete expired reservations. Returns [(user_id, lang, car_name, [numbers])]."""
    now = _now()
    with _pool.connection() as c:
        rows = c.execute(
            "DELETE FROM tickets WHERE status='reserved' AND expires_at<=%s "
            "RETURNING user_id, car_id, number",
            (now,),
        ).fetchall()
        if not rows:
            return []
        car_names = {
            r["id"]: r["name"]
            for r in c.execute(
                "SELECT id, name FROM cars WHERE id = ANY(%s)", ([r["car_id"] for r in rows],)
            ).fetchall()
        }
        langs = {
            r["id"]: r["lang"]
            for r in c.execute(
                "SELECT id, lang FROM users WHERE id = ANY(%s)", ([r["user_id"] for r in rows],)
            ).fetchall()
        }
    groups = {}
    for r in sorted(rows, key=lambda x: (x["user_id"], x["car_id"], x["number"])):
        key = (r["user_id"], r["car_id"])
        g = groups.setdefault(
            key, [r["user_id"], langs.get(r["user_id"], "en"), car_names.get(r["car_id"], "?"), []]
        )
        g[3].append(r["number"])
    return [tuple(g) for g in groups.values()]


# --------------------------------------------------------------- payments
def create_payments(user_id, file_id, kind):
    """Turn the user's live reservations into pending payments (one per car)."""
    now = _now()
    out = []
    with _pool.connection() as c:
        c.execute("SELECT 1 FROM users WHERE id=%s FOR UPDATE", (user_id,))
        rows = c.execute(
            "SELECT t.car_id, t.number, c.name, c.price FROM tickets t JOIN cars c ON c.id=t.car_id "
            "WHERE t.user_id=%s AND t.status='reserved' AND t.expires_at>%s ORDER BY t.car_id, t.number",
            (user_id, now),
        ).fetchall()
        groups = {}
        for r in rows:
            g = groups.setdefault(
                r["car_id"], {"car_name": r["name"], "price": r["price"], "numbers": []}
            )
            g["numbers"].append(r["number"])
        for car_id, g in groups.items():
            amount = g["price"] * len(g["numbers"])
            pid = c.execute(
                "INSERT INTO payments(user_id,car_id,amount,file_id,file_kind,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (user_id, car_id, amount, file_id, kind, now),
            ).fetchone()["id"]
            c.execute(
                "UPDATE tickets SET status='pending', payment_id=%s, expires_at=NULL "
                "WHERE car_id=%s AND user_id=%s AND status='reserved' AND expires_at>%s",
                (pid, car_id, user_id, now),
            )
            out.append(
                {
                    "payment_id": pid,
                    "car_id": car_id,
                    "car_name": g["car_name"],
                    "numbers": g["numbers"],
                    "amount": amount,
                }
            )
    return out


def payment_full(pid):
    d = _one(
        "SELECT p.*, c.name AS car_name, u.name AS user_name, u.username, u.phone, u.lang "
        "FROM payments p JOIN cars c ON c.id=p.car_id JOIN users u ON u.id=p.user_id WHERE p.id=%s",
        (pid,),
    )
    if not d:
        return None
    d["numbers"] = [
        x["number"]
        for x in _all("SELECT number FROM tickets WHERE payment_id=%s ORDER BY number", (pid,))
    ]
    return d


def pending_payment_ids():
    return [r["id"] for r in _all("SELECT id FROM payments WHERE status='pending' ORDER BY id")]


def user_has_pending(user_id) -> bool:
    return bool(_one("SELECT 1 AS x FROM payments WHERE user_id=%s AND status='pending' LIMIT 1", (user_id,)))


def decide_payment(pid, approve: bool, admin_id) -> bool:
    """Atomically approve/reject a pending payment. False if already decided."""
    with _pool.connection() as c:
        cur = c.execute(
            "UPDATE payments SET status=%s, decided_by=%s WHERE id=%s AND status='pending'",
            ("approved" if approve else "rejected", admin_id, pid),
        )
        if cur.rowcount == 0:
            return False
        if approve:
            c.execute("UPDATE tickets SET status='sold' WHERE payment_id=%s", (pid,))
        else:
            c.execute("DELETE FROM tickets WHERE payment_id=%s", (pid,))
    return True


def save_admin_msg(pid, chat_id, message_id):
    _run(
        "INSERT INTO admin_msgs(payment_id,chat_id,message_id) VALUES(%s,%s,%s) "
        "ON CONFLICT (payment_id, chat_id) DO UPDATE SET message_id=EXCLUDED.message_id",
        (pid, chat_id, message_id),
    )


def admin_msgs(pid):
    return _all("SELECT chat_id, message_id FROM admin_msgs WHERE payment_id=%s", (pid,))


# ------------------------------------------------------------ admin tools
def car_stats(car_id):
    res = {"sold": 0, "pending": 0, "reserved": 0}
    for r in _all(
        f"SELECT status, COUNT(*) AS c FROM tickets WHERE car_id=%s AND {LIVE} GROUP BY status",
        (car_id, _now()),
    ):
        res[r["status"]] = r["c"]
    return res


def sold_rows(car_id, include_pending=True):
    statuses = ["sold", "pending"] if include_pending else ["sold"]
    return _all(
        "SELECT t.number, t.status, u.id AS uid, u.name, u.username, u.phone "
        "FROM tickets t JOIN users u ON u.id=t.user_id "
        "WHERE t.car_id=%s AND t.status = ANY(%s) ORDER BY t.number",
        (car_id, statuses),
    )


def reset_car(car_id, admin_id):
    """Start a fresh round for one car: free ALL its numbers.

    - pending payments are rejected (returned so the caller can tell the customers)
    - approved payments are kept as history (status 'archived')
    - the car is re-opened (drawn=0, winner cleared); its draw date is NOT changed
    Other cars are never touched.
    """
    with _pool.connection() as c:
        car = c.execute("SELECT id FROM cars WHERE id=%s FOR UPDATE", (car_id,)).fetchone()
        if not car:
            return None
        pend = c.execute(
            "SELECT p.id, p.user_id, p.car_id, p.amount, p.file_kind, u.lang, u.name AS user_name, "
            "u.username, u.phone, cr.name AS car_name "
            "FROM payments p JOIN users u ON u.id=p.user_id JOIN cars cr ON cr.id=p.car_id "
            "WHERE p.car_id=%s AND p.status='pending' ORDER BY p.id",
            (car_id,),
        ).fetchall()
        for p in pend:
            p["numbers"] = [
                r["number"]
                for r in c.execute(
                    "SELECT number FROM tickets WHERE payment_id=%s ORDER BY number", (p["id"],)
                ).fetchall()
            ]
        c.execute(
            "UPDATE payments SET status='rejected', decided_by=%s WHERE car_id=%s AND status='pending'",
            (admin_id, car_id),
        )
        c.execute("UPDATE payments SET status='archived' WHERE car_id=%s AND status='approved'", (car_id,))
        removed = c.execute("DELETE FROM tickets WHERE car_id=%s RETURNING status", (car_id,)).fetchall()
        c.execute(
            "UPDATE cars SET drawn=0, winner_number=NULL, winner_user=NULL WHERE id=%s", (car_id,)
        )
    counts = {"sold": 0, "pending": 0, "reserved": 0}
    for r in removed:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"pending_payments": pend, "removed": counts}


def delete_car(car_id, admin_id):
    """Remove one car completely: its tickets, payments and admin messages.

    - pending payments are rejected first (returned, with the admin message ids, so the caller can tell
      the customers / update the admins' messages)
    Other cars are never touched. Returns None when the car does not exist.
    """
    with _pool.connection() as c:
        car = c.execute("SELECT id FROM cars WHERE id=%s FOR UPDATE", (car_id,)).fetchone()
        if not car:
            return None
        pend = c.execute(
            "SELECT p.id, p.user_id, p.car_id, p.amount, p.file_kind, u.lang, u.name AS user_name, "
            "u.username, u.phone, cr.name AS car_name "
            "FROM payments p JOIN users u ON u.id=p.user_id JOIN cars cr ON cr.id=p.car_id "
            "WHERE p.car_id=%s AND p.status='pending' ORDER BY p.id",
            (car_id,),
        ).fetchall()
        for p in pend:
            p["numbers"] = [
                r["number"]
                for r in c.execute(
                    "SELECT number FROM tickets WHERE payment_id=%s ORDER BY number", (p["id"],)
                ).fetchall()
            ]
            p["admin_msgs"] = c.execute(
                "SELECT chat_id, message_id FROM admin_msgs WHERE payment_id=%s", (p["id"],)
            ).fetchall()
        removed = c.execute("DELETE FROM tickets WHERE car_id=%s RETURNING status", (car_id,)).fetchall()
        c.execute("DELETE FROM admin_msgs WHERE payment_id IN (SELECT id FROM payments WHERE car_id=%s)", (car_id,))
        c.execute("DELETE FROM payments WHERE car_id=%s", (car_id,))
        c.execute("DELETE FROM cars WHERE id=%s", (car_id,))
    counts = {"sold": 0, "pending": 0, "reserved": 0}
    for r in removed:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"pending_payments": pend, "removed": counts}


def draw(car_id):
    """Pick a random winner among SOLD tickets and close the car. None if impossible."""
    with _pool.connection() as c:
        car = c.execute("SELECT drawn FROM cars WHERE id=%s FOR UPDATE", (car_id,)).fetchone()
        if not car or car["drawn"]:
            return None
        rows = c.execute(
            "SELECT number, user_id FROM tickets WHERE car_id=%s AND status='sold'", (car_id,)
        ).fetchall()
        if not rows:
            return None
        w = random.SystemRandom().choice(rows)
        c.execute(
            "UPDATE cars SET drawn=1, winner_number=%s, winner_user=%s WHERE id=%s",
            (w["number"], w["user_id"], car_id),
        )
    return {"number": w["number"], "user": get_user(w["user_id"])}
