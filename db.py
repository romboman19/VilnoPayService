"""
VilnoPayService — модуль роботи з PostgreSQL.
"""
import hashlib, logging, os, secrets
from pathlib import Path

import bcrypt
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://vilnopay:vilnopay@postgres:5432/vilnopay")
ADMIN_INIT_USER = os.getenv("ADMIN_INIT_USER", "admin")
ADMIN_INIT_PASS = os.getenv("ADMIN_INIT_PASS", "")

logger = logging.getLogger("vilnopay.db")


def get_pg():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def pg_query(sql, params=None, fetchone=False, fetchall=False):
    conn = get_pg()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetchone:
                return cur.fetchone()
            if fetchall:
                return cur.fetchall()
    finally:
        conn.close()


def pg_execute(sql, params=None):
    conn = get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


def init_db():
    """Створити таблиці та початкового адміна."""
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        conn = get_pg()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(schema_path.read_text())
            conn.commit()
            logger.info("DB schema applied")
        except Exception as e:
            conn.rollback()
            logger.warning("Schema apply (may already exist): %s", e)
        finally:
            conn.autocommit = True
            conn.close()

    if ADMIN_INIT_PASS:
        existing = pg_query("SELECT id FROM admin_users LIMIT 1", fetchone=True)
        if not existing:
            pw_hash = bcrypt.hashpw(ADMIN_INIT_PASS.encode(), bcrypt.gensalt()).decode()
            pg_execute(
                "INSERT INTO admin_users (username, password_hash) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (ADMIN_INIT_USER, pw_hash)
            )
            logger.info("Initial admin '%s' created", ADMIN_INIT_USER)


# ── Settings ─────────────────────────────────────────────────

def get_settings() -> dict:
    rows = pg_query("SELECT key, value FROM settings", fetchall=True)
    return {r["key"]: r["value"] for r in rows} if rows else {}


def update_settings(updates: dict):
    for key, value in updates.items():
        pg_execute(
            "UPDATE settings SET value = %s, updated_at = NOW() WHERE key = %s",
            (str(value), key)
        )


def get_link_ttl() -> int:
    s = get_settings()
    try:
        return int(s.get("link_ttl_hours", "24"))
    except (ValueError, TypeError):
        return 24


# ── API Keys ─────────────────────────────────────────────────

def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def validate_api_key(key: str | None) -> dict | None:
    if not key:
        return None
    key_hash = hash_api_key(key)
    row = pg_query(
        "SELECT id, key_prefix, label, is_active FROM api_keys WHERE key_hash = %s",
        (key_hash,), fetchone=True
    )
    if row and row["is_active"]:
        pg_execute("UPDATE api_keys SET last_used_at = NOW() WHERE id = %s", (row["id"],))
        return row
    return None


def create_api_key(label: str = "default") -> tuple[str, dict]:
    """Створити новий API-ключ. Повертає (plain_key, record)."""
    plain_key = f"vpk_{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(plain_key)
    key_prefix = plain_key[:12]
    pg_execute(
        "INSERT INTO api_keys (key_hash, key_prefix, label) VALUES (%s, %s, %s)",
        (key_hash, key_prefix, label)
    )
    row = pg_query(
        "SELECT id, key_prefix, label, is_active, created_at FROM api_keys WHERE key_hash = %s",
        (key_hash,), fetchone=True
    )
    return plain_key, row


def list_api_keys() -> list:
    return pg_query(
        "SELECT id, key_prefix, label, is_active, last_used_at, created_at FROM api_keys ORDER BY created_at DESC",
        fetchall=True
    ) or []


def revoke_api_key(key_id: int):
    pg_execute("UPDATE api_keys SET is_active = FALSE WHERE id = %s", (key_id,))


# ── Receivers ────────────────────────────────────────────────

def create_receiver(name, receiver, iban, edrpou) -> dict:
    receiver_key = f"rcv_{secrets.token_urlsafe(8)}"
    pg_execute(
        """INSERT INTO receivers (receiver_key, name, receiver, iban, edrpou)
           VALUES (%s, %s, %s, %s, %s)""",
        (receiver_key, name, receiver, iban, edrpou)
    )
    return get_receiver_by_key(receiver_key)


def get_receiver_by_key(receiver_key: str) -> dict | None:
    return pg_query(
        "SELECT * FROM receivers WHERE receiver_key = %s",
        (receiver_key,), fetchone=True
    )


def list_receivers() -> list:
    return pg_query(
        "SELECT * FROM receivers ORDER BY created_at DESC",
        fetchall=True
    ) or []


def update_receiver(receiver_key: str, name, receiver, iban, edrpou, is_active) -> dict | None:
    pg_execute(
        """UPDATE receivers SET name=%s, receiver=%s, iban=%s, edrpou=%s, is_active=%s
           WHERE receiver_key=%s""",
        (name, receiver, iban, edrpou, is_active, receiver_key)
    )
    return get_receiver_by_key(receiver_key)


def delete_receiver(receiver_key: str):
    pg_execute("DELETE FROM receivers WHERE receiver_key = %s", (receiver_key,))


# ── Admin sessions ───────────────────────────────────────────

def create_admin_session(user_id: int, ip: str, user_agent: str, ttl_hours: int) -> str:
    token = secrets.token_urlsafe(48)
    from datetime import datetime, timedelta, timezone
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    pg_execute(
        """INSERT INTO admin_sessions (user_id, token, ip_address, user_agent, expires_at)
           VALUES (%s, %s, %s, %s, %s)""",
        (user_id, token, ip, user_agent[:500] if user_agent else "", expires)
    )
    return token


def get_admin_session(token: str | None) -> dict | None:
    if not token:
        return None
    return pg_query(
        """SELECT s.id, s.user_id, u.username
           FROM admin_sessions s JOIN admin_users u ON u.id = s.user_id
           WHERE s.token = %s AND s.expires_at > NOW() AND u.is_active = TRUE""",
        (token,), fetchone=True
    )


def delete_admin_session(token: str):
    pg_execute("DELETE FROM admin_sessions WHERE token = %s", (token,))


def cleanup_expired_sessions():
    pg_execute("DELETE FROM admin_sessions WHERE expires_at < NOW()")


# ── Payment link log ────────────────────────────────────────

def log_payment_link(link_id, receiver_key, purpose, amount, api_key_prefix, ip):
    pg_execute(
        """INSERT INTO payment_links_log (link_id, receiver_key, purpose, amount, api_key_prefix, created_ip)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (link_id, receiver_key, purpose, amount, api_key_prefix, ip)
    )

# ── Page views log ──────────────────────────────────────────

def log_page_view(link_id, viewer_ip, user_agent, device_type, bank_clicked=None):
    pg_execute(
        """INSERT INTO page_views_log (link_id, viewer_ip, user_agent, device_type, bank_clicked)
           VALUES (%s, %s, %s, %s, %s)""",
        (link_id, viewer_ip, (user_agent or "")[:500], device_type, bank_clicked)
    )

def list_page_views(limit=100):
    return pg_query(
        "SELECT * FROM page_views_log ORDER BY viewed_at DESC LIMIT %s",
        (min(limit, 500),), fetchall=True
    ) or []

def list_page_views_for_link(link_id, limit=50):
    return pg_query(
        "SELECT * FROM page_views_log WHERE link_id = %s ORDER BY viewed_at DESC LIMIT %s",
        (link_id, min(limit, 200)), fetchall=True
    ) or []
