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

    # Міграції для існуючих БД
    _migrate()

    if ADMIN_INIT_PASS:
        existing = pg_query("SELECT id FROM admin_users LIMIT 1", fetchone=True)
        if not existing:
            pw_hash = bcrypt.hashpw(ADMIN_INIT_PASS.encode(), bcrypt.gensalt()).decode()
            pg_execute(
                "INSERT INTO admin_users (username, password_hash) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (ADMIN_INIT_USER, pw_hash)
            )
            logger.info("Initial admin '%s' created", ADMIN_INIT_USER)


def _migrate():
    """Міграції для існуючих БД (безпечні, idempotent)."""
    migrations = [
        # Додати новi settings якщо нема
        "INSERT INTO settings (key, value) VALUES ('logo_filename', '') ON CONFLICT (key) DO NOTHING",
        "INSERT INTO settings (key, value) VALUES ('text_color', '#101828') ON CONFLICT (key) DO NOTHING",
        "INSERT INTO settings (key, value) VALUES ('card_color', '#FFFFFF') ON CONFLICT (key) DO NOTHING",
        "INSERT INTO settings (key, value) VALUES ('border_color', '#EAECF0') ON CONFLICT (key) DO NOTHING",
        "INSERT INTO settings (key, value) VALUES ('font_family', 'Inter') ON CONFLICT (key) DO NOTHING",
        "INSERT INTO settings (key, value) VALUES ('font_size', '15') ON CONFLICT (key) DO NOTHING",
        # Таблиця переглядів
        '''CREATE TABLE IF NOT EXISTS page_views_log (
            id              SERIAL PRIMARY KEY,
            link_id         VARCHAR(32) NOT NULL,
            viewer_ip       VARCHAR(45),
            user_agent      TEXT,
            device_type     VARCHAR(20),
            bank_clicked    VARCHAR(30),
            viewed_at       TIMESTAMPTZ DEFAULT NOW()
        )''',
        "CREATE INDEX IF NOT EXISTS idx_page_views_link ON page_views_log(link_id)",
        """CREATE INDEX IF NOT EXISTS idx_page_views_viewed ON page_views_log(viewed_at)""",
        "ALTER TABLE payment_links_log ALTER COLUMN api_key_prefix TYPE VARCHAR(50)",
        # Ролі
        "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'admin'",
        # Шаблони платежів
        """CREATE TABLE IF NOT EXISTS payment_templates (
            id              SERIAL PRIMARY KEY,
            manager_id      INTEGER REFERENCES admin_users(id) ON DELETE CASCADE,
            name            VARCHAR(100) NOT NULL,
            receiver_key    VARCHAR(50) NOT NULL,
            purpose         TEXT NOT NULL,
            default_amount  VARCHAR(20),
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_templates_manager ON payment_templates(manager_id)",
    ]
    for sql in migrations:
        try:
            pg_execute(sql)
        except Exception as e:
            logger.warning("Migration skipped: %s — %s", sql[:60], e)


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
    try:
        pg_execute(
            """INSERT INTO page_views_log (link_id, viewer_ip, user_agent, device_type, bank_clicked)
               VALUES (%s, %s, %s, %s, %s)""",
            (link_id, viewer_ip, (user_agent or "")[:500], device_type, bank_clicked)
        )
    except Exception as e:
        logging.getLogger("vilnopay.db").warning("log_page_view failed (table missing?): %s", e)

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


# ── Managers CRUD ──────────────────────────────────────────

def create_manager(username, password, name=""):
    """Створити менеджера."""
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    pg_execute(
        "INSERT INTO admin_users (username, password_hash, role) VALUES (%s, %s, 'manager')",
        (username, pw_hash)
    )
    row = pg_query("SELECT id, username, role, is_active, created_at FROM admin_users WHERE username = %s AND role = 'manager'",
                    (username,), fetchone=True)
    if row and row.get("created_at"):
        row["created_at"] = str(row["created_at"])
    return row


def list_managers():
    rows = pg_query(
        "SELECT id, username, role, is_active, created_at FROM admin_users WHERE role = 'manager' ORDER BY created_at DESC",
        fetchall=True
    ) or []
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = str(r["created_at"])
    return rows


def delete_manager(manager_id):
    pg_execute("DELETE FROM admin_users WHERE id = %s AND role = 'manager'", (manager_id,))


def toggle_manager(manager_id, is_active):
    pg_execute("UPDATE admin_users SET is_active = %s WHERE id = %s AND role = 'manager'", (is_active, manager_id))


# ── Payment Templates ──────────────────────────────────────

def create_template(manager_id, name, receiver_key, purpose, default_amount=None):
    pg_execute(
        """INSERT INTO payment_templates (manager_id, name, receiver_key, purpose, default_amount)
           VALUES (%s, %s, %s, %s, %s)""",
        (manager_id, name, receiver_key, purpose, default_amount)
    )
    row = pg_query(
        "SELECT * FROM payment_templates WHERE manager_id = %s AND name = %s ORDER BY created_at DESC LIMIT 1",
        (manager_id, name), fetchone=True
    )
    if row and row.get("created_at"):
        row["created_at"] = str(row["created_at"])
    return row


def list_templates(manager_id):
    rows = pg_query(
        "SELECT * FROM payment_templates WHERE manager_id = %s ORDER BY created_at DESC",
        (manager_id,), fetchall=True
    ) or []
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = str(r["created_at"])
    return rows


def delete_template(template_id, manager_id):
    pg_execute("DELETE FROM payment_templates WHERE id = %s AND manager_id = %s", (template_id, manager_id))


# ── Manager payment history ───────────────────────────────

def list_manager_payments(manager_username, limit=50):
    """Історія платежів менеджера (по api_key_prefix або по created_ip)."""
    rows = pg_query(
        """SELECT pl.*, au.username as manager FROM payment_links_log pl
           JOIN api_keys ak ON pl.api_key_prefix = ak.key_prefix
           JOIN admin_users au ON ak.label LIKE 'manager_%%' AND au.username = REPLACE(ak.label, 'manager_', '')
           WHERE au.username = %s
           ORDER BY pl.created_at DESC LIMIT %s""",
        (manager_username, min(limit, 200)), fetchall=True
    ) or []
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = str(r["created_at"])
    return rows
