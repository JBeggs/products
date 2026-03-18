"""
Gumtree crawler SQLite schema and access helpers.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

DB_PATH = Path(__file__).resolve().parent / "gumtree_crawler.db"
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _ensure_schema()
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for DB connection."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_schema() -> None:
    """Create tables if they don't exist. Uses direct connection to avoid recursion."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS search_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                listings_found INTEGER DEFAULT 0,
                listings_new INTEGER DEFAULT 0,
                listings_updated INTEGER DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                title TEXT,
                price INTEGER,
                category TEXT,
                location TEXT,
                seller TEXT,
                condition TEXT,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                search_job_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                notes TEXT,
                ignored INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (search_job_id) REFERENCES search_jobs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_listings_ad_id ON listings(ad_id);
            CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category);
            CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen);

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                old_price INTEGER,
                new_price INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            );
            CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id);

            CREATE TABLE IF NOT EXISTS ignore_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type TEXT NOT NULL,
                value TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ignore_rules_active ON ignore_rules(active);

            CREATE TABLE IF NOT EXISTS crawler_filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT,
                updated_at TEXT NOT NULL
            );
        """)
        conn.commit()
        # Migration: add images_json if missing (safe for existing DBs)
        cur = conn.execute("PRAGMA table_info(listings)")
        cols = [r[1] for r in cur.fetchall()]
        if "images_json" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN images_json TEXT")
            conn.commit()
    finally:
        conn.close()


def init_schema() -> None:
    """Create tables if they don't exist. Call before crawl; also auto-runs on first DB access."""
    _ensure_schema()


def insert_search_job() -> int:
    """Start a search job. Returns job id."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO search_jobs (started_at, status) VALUES (?, 'running')",
            (now,),
        )
        return cur.lastrowid


def finish_search_job(
    job_id: int,
    status: str = "completed",
    listings_found: int = 0,
    listings_new: int = 0,
    listings_updated: int = 0,
    error: str | None = None,
) -> None:
    """Mark search job as finished."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        conn.execute(
            """UPDATE search_jobs SET
                finished_at = ?, status = ?, listings_found = ?, listings_new = ?,
                listings_updated = ?, error = ?
            WHERE id = ?""",
            (now, status, listings_found, listings_new, listings_updated, error, job_id),
        )


def get_listing_by_ad_id(ad_id: str) -> dict | None:
    """Get listing by ad_id."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM listings WHERE ad_id = ?", (ad_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def upsert_listing(
    ad_id: str,
    url: str,
    title: str | None,
    price: int | None,
    category: str | None,
    location: str | None,
    seller: str | None,
    condition: str | None,
    description: str | None,
    search_job_id: int | None,
) -> tuple[int, bool]:
    """
    Insert or update listing. Returns (listing_id, is_new).
    If price changed, inserts into price_history.
    """
    now = datetime.utcnow().isoformat() + "Z"
    existing = get_listing_by_ad_id(ad_id)
    if existing:
        listing_id = existing["id"]
        old_price = existing["price"]
        price_changed = (old_price != price) if (old_price is not None or price is not None) else False
        if price_changed and price is not None:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES (?, ?, ?, ?)",
                    (listing_id, old_price, price, now),
                )
        with get_db() as conn:
            conn.execute(
                """UPDATE listings SET
                    url = ?, title = ?, price = ?, category = ?, location = ?,
                    seller = ?, condition = ?, description = ?, last_seen = ?,
                    search_job_id = ?, updated_at = ?
                WHERE id = ?""",
                (url, title, price, category, location, seller, condition, description, now, search_job_id, now, listing_id),
            )
        return listing_id, False
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO listings (ad_id, url, title, price, category, location, seller, condition, description, first_seen, last_seen, search_job_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ad_id, url, title, price, category, location, seller, condition, description, now, now, search_job_id, now, now),
        )
        return cur.lastrowid, True


def get_active_ignore_rules() -> list[dict]:
    """Get all active ignore rules."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM ignore_rules WHERE active = 1")
        return [dict(r) for r in cur.fetchall()]


def listing_matches_ignore(listing: dict, rules: list[dict]) -> bool:
    """Check if listing matches any ignore rule."""
    url = (listing.get("url") or "").lower()
    ad_id = str(listing.get("ad_id") or "")
    title = (listing.get("title") or "").lower()
    seller = (listing.get("seller") or "").lower()
    for r in rules:
        t = (r.get("rule_type") or "").lower()
        v = (r.get("value") or "").strip().lower()
        if not v:
            continue
        if t == "url" and v in url:
            return True
        if t == "ad_id" and v == ad_id.lower():
            return True
        if t == "title_keyword" and v in title:
            return True
        if t == "seller" and v in seller:
            return True
    return False


def get_crawler_filters() -> dict[str, Any]:
    """Get crawler filter key-value pairs."""
    with get_db() as conn:
        cur = conn.execute("SELECT key, value FROM crawler_filters")
        return {r["key"]: r["value"] for r in cur.fetchall()}


def set_crawler_filter(key: str, value: str | None) -> None:
    """Set a crawler filter."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO crawler_filters (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
            (key, value, now, value, now),
        )


def list_listings(
    category: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    keyword: str | None = None,
    location: str | None = None,
    seller: str | None = None,
    new_today: bool | None = None,
    price_changed: bool | None = None,
    include_ignored: bool = False,
    sort: str = "last_seen",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    List listings with filters. Returns (rows, total_count).
    """
    conditions = []
    params: list[Any] = []
    if not include_ignored:
        conditions.append("ignored = 0")
    if category:
        conditions.append("category = ?")
        params.append(category)
    if min_price is not None:
        conditions.append("price >= ?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("price <= ?")
        params.append(max_price)
    if keyword:
        conditions.append("(title LIKE ? OR description LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if location:
        conditions.append("location LIKE ?")
        params.append(f"%{location}%")
    if seller:
        conditions.append("seller LIKE ?")
        params.append(f"%{seller}%")
    if new_today:
        conditions.append("date(first_seen) = date('now', 'localtime')")
    if price_changed:
        conditions.append("id IN (SELECT listing_id FROM price_history)")

    where = " AND ".join(conditions) if conditions else "1=1"
    valid_sort = {"last_seen", "first_seen", "price", "title", "created_at"}
    sort_col = sort if sort in valid_sort else "last_seen"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    with get_db() as conn:
        cur = conn.execute(f"SELECT COUNT(*) FROM listings WHERE {where}", params)
        total = cur.fetchone()[0]
        prev_price_sql = "(SELECT ph.old_price FROM price_history ph WHERE ph.listing_id = listings.id ORDER BY ph.changed_at DESC LIMIT 1)"
        cur = conn.execute(
            f"SELECT listings.*, {prev_price_sql} AS prev_price FROM listings WHERE {where} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows, total


def get_price_changes(limit: int = 50) -> list[dict]:
    """Get recent price changes with listing info."""
    with get_db() as conn:
        cur = conn.execute("""
            SELECT ph.id, ph.listing_id, ph.old_price, ph.new_price, ph.changed_at,
                   l.ad_id, l.url, l.title, l.category
            FROM price_history ph
            JOIN listings l ON l.id = ph.listing_id
            ORDER BY ph.changed_at DESC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_listing_by_id(listing_id: int) -> dict | None:
    """Get listing by id."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_listing_images(listing_id: int, image_paths: list[str]) -> bool:
    """Store fetched image paths (relative, e.g. images/foo.jpg) on listing. Returns True if updated."""
    import json
    val = json.dumps(image_paths) if image_paths else None
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE listings SET images_json = ?, updated_at = ? WHERE id = ?",
            (val, now, listing_id),
        )
        return cur.rowcount > 0


def patch_listing(listing_id: int, notes: str | None = None, ignored: int | None = None) -> dict | None:
    """Update listing notes and/or ignored flag. Returns updated listing or None."""
    updates = []
    params: list[Any] = []
    if notes is not None:
        updates.append("notes = ?")
        params.append(notes)
    if ignored is not None:
        updates.append("ignored = ?")
        params.append(1 if ignored else 0)
    if not updates:
        return get_listing_by_id(listing_id)
    now = datetime.utcnow().isoformat() + "Z"
    updates.append("updated_at = ?")
    params.append(now)
    params.append(listing_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE listings SET {', '.join(updates)} WHERE id = ?",
            params,
        )
    return get_listing_by_id(listing_id)


# --- Ignore rules CRUD ---
def list_ignore_rules(active_only: bool = False) -> list[dict]:
    """List ignore rules."""
    with get_db() as conn:
        if active_only:
            cur = conn.execute("SELECT * FROM ignore_rules WHERE active = 1 ORDER BY id")
        else:
            cur = conn.execute("SELECT * FROM ignore_rules ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def create_ignore_rule(rule_type: str, value: str) -> int:
    """Create ignore rule. Returns id."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO ignore_rules (rule_type, value, active, created_at) VALUES (?, ?, 1, ?)",
            (rule_type, value.strip(), now),
        )
        return cur.lastrowid


def get_ignore_rule(rule_id: int) -> dict | None:
    """Get ignore rule by id."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM ignore_rules WHERE id = ?", (rule_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_ignore_rule(rule_id: int, rule_type: str | None = None, value: str | None = None, active: int | None = None) -> dict | None:
    """Update ignore rule."""
    updates = []
    params: list[Any] = []
    if rule_type is not None:
        updates.append("rule_type = ?")
        params.append(rule_type)
    if value is not None:
        updates.append("value = ?")
        params.append(value)
    if active is not None:
        updates.append("active = ?")
        params.append(active)
    if not updates:
        return get_ignore_rule(rule_id)
    params.append(rule_id)
    with get_db() as conn:
        conn.execute(f"UPDATE ignore_rules SET {', '.join(updates)} WHERE id = ?", params)
    return get_ignore_rule(rule_id)


def delete_ignore_rule(rule_id: int) -> bool:
    """Delete ignore rule. Returns True if deleted."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM ignore_rules WHERE id = ?", (rule_id,))
        return cur.rowcount > 0


# --- Crawler filters CRUD ---
def list_crawler_filters() -> list[dict]:
    """List all crawler filters."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM crawler_filters ORDER BY key")
        return [dict(r) for r in cur.fetchall()]


def get_crawler_filter(key: str) -> dict | None:
    """Get crawler filter by key."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM crawler_filters WHERE key = ?", (key,))
        row = cur.fetchone()
        return dict(row) if row else None


def delete_crawler_filter(key: str) -> bool:
    """Delete crawler filter. Returns True if deleted."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM crawler_filters WHERE key = ?", (key,))
        return cur.rowcount > 0


# --- Search jobs ---
def get_last_search_job() -> dict | None:
    """Get most recent search job."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM search_jobs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None
