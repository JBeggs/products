"""
Gumtree crawler SQLite schema and access helpers.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from .config import get_default_location_preferences, get_default_scenarios

DB_PATH = Path(__file__).resolve().parent / "gumtree_crawler.db"
_local = threading.local()


def _utcnow() -> str:
    return datetime.utcnow().isoformat() + "Z"


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


def _ensure_columns(conn: sqlite3.Connection, table: str, required: dict[str, str]) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}
    for name, decl in required.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _ensure_schema() -> None:
    """Create tables if they don't exist. Uses direct connection to avoid recursion."""

    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    try:
        conn.executescript(
            """
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
                posted_at TEXT,
                attributes_json TEXT,
                signals_json TEXT,
                images_json TEXT,
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

            CREATE TABLE IF NOT EXISTS scenario_configs (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scenario_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                scenario_slug TEXT NOT NULL,
                search_job_id INTEGER,
                visible INTEGER NOT NULL DEFAULT 0,
                match_score REAL NOT NULL DEFAULT 0,
                price_score REAL NOT NULL DEFAULT 0,
                urgency_score REAL NOT NULL DEFAULT 0,
                special_state TEXT,
                reasons_json TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(listing_id, scenario_slug),
                FOREIGN KEY (listing_id) REFERENCES listings(id),
                FOREIGN KEY (scenario_slug) REFERENCES scenario_configs(slug),
                FOREIGN KEY (search_job_id) REFERENCES search_jobs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_scenario_matches_slug_visible ON scenario_matches(scenario_slug, visible);
            CREATE INDEX IF NOT EXISTS idx_scenario_matches_listing ON scenario_matches(listing_id);
        """
        )
        _ensure_columns(
            conn,
            "listings",
            {
                "posted_at": "TEXT",
                "attributes_json": "TEXT",
                "signals_json": "TEXT",
                "images_json": "TEXT",
            },
        )
        conn.commit()
    finally:
        conn.close()


def init_schema() -> None:
    """Create tables if they don't exist, then seed default config."""

    _ensure_schema()
    ensure_default_config()


def ensure_default_config() -> None:
    """Seed default scenarios and location preferences if missing."""

    for scenario in get_default_scenarios():
        upsert_scenario_config(scenario, update_existing=False)
    if get_location_preferences() is None:
        save_location_preferences(get_default_location_preferences())


def insert_search_job() -> int:
    """Start a search job. Returns job id."""

    now = _utcnow()
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

    now = _utcnow()
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


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


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
    posted_at: str | None = None,
    attributes: dict[str, Any] | None = None,
    signals: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """
    Insert or update listing. Returns (listing_id, is_new).
    If price changed, inserts into price_history.
    """

    now = _utcnow()
    existing = get_listing_by_ad_id(ad_id)
    attributes_json = _json_dumps(attributes or {})
    signals_json = _json_dumps(signals or {})
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
                    seller = ?, condition = ?, description = ?, posted_at = ?,
                    attributes_json = ?, signals_json = ?, last_seen = ?,
                    search_job_id = ?, updated_at = ?
                WHERE id = ?""",
                (
                    url,
                    title,
                    price,
                    category,
                    location,
                    seller,
                    condition,
                    description,
                    posted_at,
                    attributes_json,
                    signals_json,
                    now,
                    search_job_id,
                    now,
                    listing_id,
                ),
            )
        return listing_id, False
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO listings (
                ad_id, url, title, price, category, location, seller, condition,
                description, first_seen, last_seen, search_job_id, created_at,
                updated_at, posted_at, attributes_json, signals_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ad_id,
                url,
                title,
                price,
                category,
                location,
                seller,
                condition,
                description,
                now,
                now,
                search_job_id,
                now,
                now,
                posted_at,
                attributes_json,
                signals_json,
            ),
        )
        return cur.lastrowid, True


def upsert_scenario_match(
    listing_id: int,
    scenario_slug: str,
    search_job_id: int | None,
    visible: bool,
    match_score: float,
    price_score: float,
    urgency_score: float,
    special_state: str | None,
    reasons: list[str] | None,
) -> None:
    """Insert or update one listing-to-scenario evaluation."""

    now = _utcnow()
    reasons_json = _json_dumps(reasons or [])
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scenario_matches (
                listing_id, scenario_slug, search_job_id, visible, match_score,
                price_score, urgency_score, special_state, reasons_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id, scenario_slug) DO UPDATE SET
                search_job_id = excluded.search_job_id,
                visible = excluded.visible,
                match_score = excluded.match_score,
                price_score = excluded.price_score,
                urgency_score = excluded.urgency_score,
                special_state = excluded.special_state,
                reasons_json = excluded.reasons_json,
                updated_at = excluded.updated_at
            """,
            (
                listing_id,
                scenario_slug,
                search_job_id,
                1 if visible else 0,
                float(match_score),
                float(price_score),
                float(urgency_score),
                special_state,
                reasons_json,
                now,
            ),
        )


def upsert_scenario_config(scenario: dict[str, Any], update_existing: bool = False) -> None:
    """Insert a scenario config. Existing rows are preserved unless requested."""

    now = _utcnow()
    slug = (scenario.get("slug") or "").strip()
    name = (scenario.get("name") or slug).strip()
    enabled = 1 if scenario.get("enabled", True) else 0
    config_json = _json_dumps(scenario) or "{}"
    with get_db() as conn:
        if update_existing:
            conn.execute(
                """
                INSERT INTO scenario_configs (slug, name, enabled, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    enabled = excluded.enabled,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (slug, name, enabled, config_json, now, now),
            )
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO scenario_configs (slug, name, enabled, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (slug, name, enabled, config_json, now, now),
            )


def list_scenario_configs(enabled_only: bool = False) -> list[dict[str, Any]]:
    """Return all scenario configs with parsed JSON."""

    sql = "SELECT * FROM scenario_configs"
    params: list[Any] = []
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    with get_db() as conn:
        cur = conn.execute(sql, params)
        rows = []
        for row in cur.fetchall():
            data = dict(row)
            cfg = json.loads(data.get("config_json") or "{}")
            cfg["enabled"] = bool(data.get("enabled"))
            rows.append(cfg)
        return rows


def get_scenario_config(slug: str) -> dict[str, Any] | None:
    """Get one scenario config by slug."""

    with get_db() as conn:
        cur = conn.execute("SELECT * FROM scenario_configs WHERE slug = ?", (slug,))
        row = cur.fetchone()
        if not row:
            return None
        data = dict(row)
        cfg = json.loads(data.get("config_json") or "{}")
        cfg["enabled"] = bool(data.get("enabled"))
        return cfg


def save_scenario_config(slug: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Merge and save one scenario config."""

    current = get_scenario_config(slug)
    if not current:
        return None
    merged = dict(current)
    merged.update(updates or {})
    if "slug" not in merged:
        merged["slug"] = slug
    upsert_scenario_config(merged, update_existing=True)
    return get_scenario_config(slug)


def get_scenario_counts() -> dict[str, dict[str, int]]:
    """Return visible/total counts per scenario."""

    with get_db() as conn:
        cur = conn.execute(
            """
            SELECT sc.slug,
                   COALESCE(SUM(CASE WHEN sm.visible = 1 THEN 1 ELSE 0 END), 0) AS visible_count,
                   COALESCE(COUNT(sm.id), 0) AS total_count
            FROM scenario_configs sc
            LEFT JOIN scenario_matches sm ON sm.scenario_slug = sc.slug
            GROUP BY sc.slug
            """
        )
        return {
            row["slug"]: {
                "visible_count": int(row["visible_count"] or 0),
                "total_count": int(row["total_count"] or 0),
            }
            for row in cur.fetchall()
        }


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
    for rule in rules:
        rule_type = (rule.get("rule_type") or "").lower()
        value = (rule.get("value") or "").strip().lower()
        if not value:
            continue
        if rule_type == "url" and value in url:
            return True
        if rule_type == "ad_id" and value == ad_id.lower():
            return True
        if rule_type == "title_keyword" and value in title:
            return True
        if rule_type == "seller" and value in seller:
            return True
    return False


def get_crawler_filters() -> dict[str, Any]:
    """Get crawler filter key-value pairs."""

    with get_db() as conn:
        cur = conn.execute("SELECT key, value FROM crawler_filters")
        return {r["key"]: r["value"] for r in cur.fetchall()}


def set_crawler_filter(key: str, value: str | None) -> None:
    """Set a crawler filter."""

    now = _utcnow()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO crawler_filters (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )


def get_json_filter(key: str) -> Any:
    """Read one JSON-encoded crawler filter."""

    row = get_crawler_filter(key)
    if not row or row.get("value") in (None, ""):
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def set_json_filter(key: str, value: Any) -> None:
    """Persist one JSON-encodable crawler filter."""

    set_crawler_filter(key, _json_dumps(value))


def get_location_preferences() -> dict[str, Any] | None:
    """Get location preferences JSON."""

    return get_json_filter("location_preferences")


def save_location_preferences(value: dict[str, Any]) -> dict[str, Any]:
    """Save location preferences JSON."""

    set_json_filter("location_preferences", value)
    return get_location_preferences() or {}


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
    scenario_slug: str | None = None,
) -> tuple[list[dict], int]:
    """
    List listings with filters. Returns (rows, total_count).
    When scenario_slug is provided, only visible matches for that scenario are returned.
    """

    conditions = []
    params: list[Any] = []
    if not include_ignored:
        conditions.append("l.ignored = 0")
    if category:
        conditions.append("l.category = ?")
        params.append(category)
    if min_price is not None:
        conditions.append("l.price >= ?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("l.price <= ?")
        params.append(max_price)
    if keyword:
        conditions.append("(l.title LIKE ? OR l.description LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if location:
        conditions.append("l.location LIKE ?")
        params.append(f"%{location}%")
    if seller:
        conditions.append("l.seller LIKE ?")
        params.append(f"%{seller}%")
    if new_today:
        conditions.append("date(l.first_seen) = date('now', 'localtime')")
    if price_changed:
        conditions.append("l.id IN (SELECT listing_id FROM price_history)")

    if scenario_slug:
        join_sql = """
            JOIN scenario_matches sm
              ON sm.listing_id = l.id
             AND sm.scenario_slug = ?
             AND sm.visible = 1
        """
        params = [scenario_slug] + params
        extra_select = """
            sm.visible AS scenario_visible,
            sm.match_score,
            sm.price_score,
            sm.urgency_score,
            sm.special_state,
            sm.reasons_json,
            sc.name AS scenario_name,
            sc.slug AS scenario_slug,
            '' AS scenario_slugs
        """
        join_sql += " LEFT JOIN scenario_configs sc ON sc.slug = sm.scenario_slug"
    else:
        join_sql = """
            LEFT JOIN (
                SELECT
                    listing_id,
                    GROUP_CONCAT(CASE WHEN visible = 1 THEN scenario_slug END) AS scenario_slugs,
                    MAX(match_score) AS best_match_score,
                    MAX(price_score) AS best_price_score,
                    MAX(urgency_score) AS best_urgency_score
                FROM scenario_matches
                GROUP BY listing_id
            ) agg ON agg.listing_id = l.id
        """
        extra_select = """
            NULL AS scenario_visible,
            COALESCE(agg.best_match_score, 0) AS match_score,
            COALESCE(agg.best_price_score, 0) AS price_score,
            COALESCE(agg.best_urgency_score, 0) AS urgency_score,
            NULL AS special_state,
            NULL AS reasons_json,
            NULL AS scenario_name,
            NULL AS scenario_slug,
            COALESCE(agg.scenario_slugs, '') AS scenario_slugs
        """

    where = " AND ".join(conditions) if conditions else "1=1"
    valid_sort = {"last_seen", "first_seen", "price", "title", "created_at", "match_score", "urgency_score", "price_score"}
    sort_map = {
        "last_seen": "l.last_seen",
        "first_seen": "l.first_seen",
        "price": "l.price",
        "title": "l.title",
        "created_at": "l.created_at",
        "match_score": "match_score",
        "urgency_score": "urgency_score",
        "price_score": "price_score",
    }
    sort_col = sort_map.get(sort, "l.last_seen") if sort in valid_sort else "l.last_seen"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    prev_price_sql = "(SELECT ph.old_price FROM price_history ph WHERE ph.listing_id = l.id ORDER BY ph.changed_at DESC LIMIT 1)"
    base_sql = f"""
        FROM listings l
        {join_sql}
        WHERE {where}
    """
    with get_db() as conn:
        cur = conn.execute(f"SELECT COUNT(*) {base_sql}", params)
        total = cur.fetchone()[0]
        cur = conn.execute(
            f"""
            SELECT
                l.*,
                {prev_price_sql} AS prev_price,
                {extra_select}
            {base_sql}
            ORDER BY {sort_col} {order_dir}, l.last_seen DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows, total


def get_price_changes(limit: int = 50, scenario_slug: str | None = None) -> list[dict]:
    """Get recent price changes with listing info."""

    params: list[Any] = []
    join_sql = ""
    where_sql = ""
    if scenario_slug:
        join_sql = """
            JOIN scenario_matches sm
              ON sm.listing_id = l.id
             AND sm.scenario_slug = ?
             AND sm.visible = 1
        """
        params.append(scenario_slug)
    params.append(limit)
    with get_db() as conn:
        cur = conn.execute(
            f"""
            SELECT ph.id, ph.listing_id, ph.old_price, ph.new_price, ph.changed_at,
                   l.ad_id, l.url, l.title, l.category
            FROM price_history ph
            JOIN listings l ON l.id = ph.listing_id
            {join_sql}
            {where_sql}
            ORDER BY ph.changed_at DESC
            LIMIT ?
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def get_listing_by_id(listing_id: int) -> dict | None:
    """Get listing by id."""

    with get_db() as conn:
        cur = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_listing_images(listing_id: int, image_paths: list[str]) -> bool:
    """Store fetched image paths (relative, e.g. images/foo.jpg) on listing. Returns True if updated."""

    now = _utcnow()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE listings SET images_json = ?, updated_at = ? WHERE id = ?",
            (_json_dumps(image_paths or []), now, listing_id),
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
    now = _utcnow()
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

    now = _utcnow()
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
