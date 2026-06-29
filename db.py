"""
VilnoPayService — модуль роботи з PostgreSQL.
"""
import hashlib, logging, os, secrets
from pathlib import Path

import bcrypt
import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://vilnopay:vilnopay@postgres:5432/vilnopay")
ADMIN_INIT_USER = os.getenv("ADMIN_INIT_USER", "admin")
ADMIN_INIT_PASS = os.getenv("ADMIN_INIT_PASS", "")

logger = logging.getLogger("vilnopay.db")

_pool = None

def _get_pool():
    global _pool
    if _pool is None:
        _pool = pg_pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=DATABASE_URL
        )
    return _pool

@contextmanager
def get_pg():
    p = _get_pool()
    conn = p.getconn()
    conn.autocommit = True
    try:
        yield conn
    finally:
        p.putconn(conn)


def pg_query(sql, params=None, fetchone=False, fetchall=False):
    with get_pg() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetchone:
                return cur.fetchone()
            if fetchall:
                return cur.fetchall()


def pg_execute(sql, params=None):
    with get_pg() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def init_db():
    """Створити таблиці та початкового адміна."""
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        with get_pg() as conn:
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
        # LiqPay поля в receivers
        "ALTER TABLE receivers ADD COLUMN IF NOT EXISTS liqpay_public_key TEXT DEFAULT ''",
        "ALTER TABLE receivers ADD COLUMN IF NOT EXISTS liqpay_private_enc TEXT DEFAULT ''",
        "ALTER TABLE receivers ADD COLUMN IF NOT EXISTS liqpay_display_mode VARCHAR(30) DEFAULT ''",
        "ALTER TABLE receivers ADD COLUMN IF NOT EXISTS liqpay_pay_methods TEXT DEFAULT '[\"card\",\"privat24\",\"wallet\"]'",
        "ALTER TABLE receivers ADD COLUMN IF NOT EXISTS liqpay_sandbox BOOLEAN DEFAULT FALSE",
        # Зробити provider_id nullable (LiqPay прив'язаний до отримувача, не до провайдера)
        "ALTER TABLE liqpay_transactions ALTER COLUMN provider_id DROP NOT NULL",
        # Payment providers
        """CREATE TABLE IF NOT EXISTS payment_providers (
            id              SERIAL PRIMARY KEY,
            provider_type   VARCHAR(50) NOT NULL,
            name            VARCHAR(200) NOT NULL,
            is_active       BOOLEAN DEFAULT FALSE,
            public_key      TEXT NOT NULL DEFAULT '',
            private_key_enc TEXT NOT NULL DEFAULT '',
            display_mode    VARCHAR(30) NOT NULL DEFAULT 'widget',
            pay_methods     TEXT DEFAULT '["card","privat24","wallet"]',
            is_sandbox      BOOLEAN DEFAULT FALSE,
            extra_config    TEXT DEFAULT '{}',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_providers_type ON payment_providers(provider_type)",
        "CREATE INDEX IF NOT EXISTS idx_providers_active ON payment_providers(is_active)",
        # LiqPay transactions
        """CREATE TABLE IF NOT EXISTS liqpay_transactions (
            id              SERIAL PRIMARY KEY,
            link_id         VARCHAR(32) NOT NULL,
            order_id        VARCHAR(100) NOT NULL UNIQUE,
            provider_id     INTEGER REFERENCES payment_providers(id),
            liqpay_order_id VARCHAR(100),
            status          VARCHAR(30),
            amount          NUMERIC(12,2),
            currency        VARCHAR(5) DEFAULT 'UAH',
            sender_card     VARCHAR(20),
            transaction_id  BIGINT,
            callback_data   TEXT,
            callback_ip     VARCHAR(45),
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_liqpay_tx_link ON liqpay_transactions(link_id)",
        "CREATE INDEX IF NOT EXISTS idx_liqpay_tx_order ON liqpay_transactions(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_liqpay_tx_status ON liqpay_transactions(status)",
        # block_order setting
        "INSERT INTO settings (key, value) VALUES ('block_order', '[\"nbu_qr\",\"liqpay\",\"requisites\"]') ON CONFLICT (key) DO NOTHING",
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

def create_receiver(name, receiver, iban, edrpou,
                  liqpay_public_key="", liqpay_private_key="", liqpay_display_mode="",
                  liqpay_pay_methods='["card","privat24","wallet"]', liqpay_sandbox=False) -> dict:
    receiver_key = f"rcv_{secrets.token_urlsafe(8)}"
    enc = encrypt_private_key(liqpay_private_key) if liqpay_private_key else ""
    pg_execute(
        """INSERT INTO receivers (receiver_key, name, receiver, iban, edrpou,
           liqpay_public_key, liqpay_private_enc, liqpay_display_mode, liqpay_pay_methods, liqpay_sandbox)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (receiver_key, name, receiver, iban, edrpou,
         liqpay_public_key, enc, liqpay_display_mode, liqpay_pay_methods, liqpay_sandbox)
    )
    return get_receiver_by_key(receiver_key)


def get_receiver_by_key(receiver_key: str) -> dict | None:
    return pg_query(
        """SELECT id, receiver_key, name, receiver, iban, edrpou, is_active,
           liqpay_public_key, liqpay_display_mode, liqpay_pay_methods, liqpay_sandbox,
           created_at, updated_at
           FROM receivers WHERE receiver_key = %s""",
        (receiver_key,), fetchone=True
    )


def list_receivers() -> list:
    return pg_query(
        """SELECT id, receiver_key, name, receiver, iban, edrpou, is_active,
           liqpay_public_key, liqpay_display_mode, liqpay_pay_methods, liqpay_sandbox,
           created_at, updated_at
           FROM receivers ORDER BY created_at DESC""",
        fetchall=True
    ) or []


def update_receiver(receiver_key: str, **kwargs) -> dict | None:
    allowed = {"name", "receiver", "iban", "edrpou", "is_active",
               "liqpay_public_key", "liqpay_display_mode", "liqpay_pay_methods", "liqpay_sandbox"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if "liqpay_private_key" in kwargs and kwargs["liqpay_private_key"]:
        updates["liqpay_private_enc"] = encrypt_private_key(kwargs["liqpay_private_key"])
    if not updates:
        return get_receiver_by_key(receiver_key)
    sets = ", ".join(f"{k}=%s" for k in updates)
    vals = list(updates.values()) + [receiver_key]
    pg_execute(f"UPDATE receivers SET {sets} WHERE receiver_key=%s", vals)
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
        """SELECT s.id, s.user_id, u.username, u.role
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
    """Створити менеджера + API ключ."""
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    pg_execute(
        "INSERT INTO admin_users (username, password_hash, role) VALUES (%s, %s, 'manager')",
        (username, pw_hash)
    )
    row = pg_query("SELECT id, username, role, is_active, created_at FROM admin_users WHERE username = %s AND role = 'manager'",
                    (username,), fetchone=True)
    if row and row.get("created_at"):
        row["created_at"] = str(row["created_at"])
    # Створити API ключ для менеджера
    plain_key, key_record = create_api_key(f"manager_{username}")
    row["api_key"] = plain_key
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
    """Історія платежів менеджера — по API ключу з label manager_<username>."""
    rows = pg_query(
        """SELECT pl.* FROM payment_links_log pl
           JOIN api_keys ak ON pl.api_key_prefix = ak.key_prefix
           WHERE ak.label = %s
           ORDER BY pl.created_at DESC LIMIT %s""",
        (f"manager_{manager_username}", min(limit, 200)), fetchall=True
    ) or []
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = str(r["created_at"])
    return rows

# ── Manager API key retrieval ──────────────────────────────

def get_manager_api_key_record(manager_username):
    """Повертає запис API ключа менеджера (без повного ключа)."""
    row = pg_query(
        "SELECT id, key_prefix, label, is_active, last_used_at, created_at FROM api_keys WHERE label = %s ORDER BY created_at DESC LIMIT 1",
        (f"manager_{manager_username}",), fetchone=True
    )
    if row:
        for k in ("last_used_at", "created_at"):
            if row.get(k):
                row[k] = str(row[k])
    return row





# ── Receiver LiqPay decryption ───────────────────────────────

def get_receiver_liqpay_private(receiver_key):
    """Повертає розшифрований LiqPay private key отримувача (тiльки для підпису)."""
    row = pg_query(
        "SELECT liqpay_private_enc FROM receivers WHERE receiver_key = %s",
        (receiver_key,), fetchone=True)
    if row and row.get("liqpay_private_enc"):
        return decrypt_private_key(row["liqpay_private_enc"])
    return ""


# ── Payment Providers (LiqPay) ──────────────────────────────

PROVIDER_ENCRYPTION_KEY = os.getenv("PROVIDER_ENCRYPTION_KEY", "")


def _get_fernet():
    if not PROVIDER_ENCRYPTION_KEY:
        raise ValueError("PROVIDER_ENCRYPTION_KEY не налаштований")
    from cryptography.fernet import Fernet
    return Fernet(PROVIDER_ENCRYPTION_KEY.encode())


def encrypt_private_key(plain_key: str) -> str:
    if not plain_key:
        return ""
    return _get_fernet().encrypt(plain_key.encode()).decode()


def decrypt_private_key(encrypted_key: str) -> str:
    if not encrypted_key:
        return ""
    return _get_fernet().decrypt(encrypted_key.encode()).decode()


def create_provider(provider_type, name, public_key, private_key,
                    display_mode="widget",
                    pay_methods='["card","privat24","wallet"]',
                    is_sandbox=False, extra_config="{}"):
    enc = encrypt_private_key(private_key)
    pg_execute(
        """INSERT INTO payment_providers
           (provider_type,name,public_key,private_key_enc,display_mode,
            pay_methods,is_sandbox,extra_config)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (provider_type, name, public_key, enc, display_mode,
         pay_methods, is_sandbox, extra_config))
    return get_provider_by_type(provider_type)


def get_provider_by_type(provider_type):
    row = pg_query(
        """SELECT id,provider_type,name,is_active,public_key,display_mode,
           pay_methods,is_sandbox,extra_config FROM payment_providers
           WHERE provider_type=%s ORDER BY created_at DESC LIMIT 1""",
        (provider_type,), fetchone=True)
    if row:
        for k in ("created_at", "updated_at"):
            if row.get(k): row[k] = str(row[k])
    return row


def get_active_providers():
    rows = pg_query(
        """SELECT id,provider_type,name,public_key,display_mode,
           pay_methods,is_sandbox,extra_config
           FROM payment_providers WHERE is_active=TRUE ORDER BY id""",
        fetchall=True) or []
    return rows


def list_providers():
    rows = pg_query(
        """SELECT id,provider_type,name,is_active,public_key,display_mode,
           pay_methods,is_sandbox,extra_config,created_at,updated_at
           FROM payment_providers ORDER BY created_at DESC""",
        fetchall=True) or []
    for r in rows:
        for k in ("created_at", "updated_at"):
            if r.get(k): r[k] = str(r[k])
    return rows


def update_provider(provider_id, **kwargs):
    allowed = {"name", "is_active", "public_key", "display_mode",
               "pay_methods", "is_sandbox", "extra_config"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if "private_key" in kwargs and kwargs["private_key"]:
        updates["private_key_enc"] = encrypt_private_key(kwargs["private_key"])
    if not updates:
        return
    sets = ", ".join(f"{k}=%s" for k in updates)
    vals = list(updates.values()) + [provider_id]
    pg_execute(f"UPDATE payment_providers SET {sets} WHERE id=%s", vals)


def delete_provider(pid):
    pg_execute("DELETE FROM payment_providers WHERE id=%s", (pid,))


def get_provider_decrypted(provider_id):
    row = pg_query(
        "SELECT * FROM payment_providers WHERE id=%s AND is_active=TRUE",
        (provider_id,), fetchone=True)
    if row and row.get("private_key_enc"):
        row["private_key"] = decrypt_private_key(row["private_key_enc"])
    if row:
        for k in ("created_at", "updated_at"):
            if row.get(k): row[k] = str(row[k])
    return row


# ── LiqPay Transactions ─────────────────────────────────────

def create_liqpay_tx(link_id, order_id, provider_id):
    pg_execute(
        "INSERT INTO liqpay_transactions (link_id,order_id,provider_id) VALUES (%s,%s,%s)",
        (link_id, order_id, provider_id if provider_id else None))


def update_liqpay_tx(order_id, **kwargs):
    allowed = {"status", "liqpay_order_id", "amount", "currency",
               "sender_card", "transaction_id", "callback_data", "callback_ip"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=%s" for k in updates)
    vals = list(updates.values()) + [order_id]
    pg_execute(f"UPDATE liqpay_transactions SET {sets} WHERE order_id=%s", vals)


def get_liqpay_tx_by_link(link_id):
    row = pg_query(
        "SELECT * FROM liqpay_transactions WHERE link_id=%s ORDER BY created_at DESC LIMIT 1",
        (link_id,), fetchone=True)
    if row:
        for k in ("created_at", "updated_at"):
            if row.get(k): row[k] = str(row[k])
    return row


def list_liqpay_transactions(limit=100):
    rows = pg_query(
        "SELECT * FROM liqpay_transactions ORDER BY created_at DESC LIMIT %s",
        (min(limit, 500),), fetchall=True) or []
    for r in rows:
        for k in ("created_at", "updated_at"):
            if r.get(k): r[k] = str(r[k])
    return rows
