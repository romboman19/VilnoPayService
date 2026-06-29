-- ============================================================
-- VilnoPayService v4.0 — PostgreSQL Schema
-- Адмін-панель, отримувачі, API-ключі, брендинг
-- ============================================================

-- Розширення
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Адмін-користувачі ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(100) UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,             -- bcrypt hash
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Сесії адміна ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_sessions (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    token           VARCHAR(128) UNIQUE NOT NULL,
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_admin_sessions_token ON admin_sessions(token);
CREATE INDEX idx_admin_sessions_expires ON admin_sessions(expires_at);

-- ── Налаштування брендингу (key-value) ──────────────────────
-- Ключі: logo_filename, bg_color, primary_color, accent_color,
--         page_title, page_subtitle, footer_text,
--         link_ttl_hours, custom_css
CREATE TABLE IF NOT EXISTS settings (
    id              SERIAL PRIMARY KEY,
    key             VARCHAR(100) UNIQUE NOT NULL,
    value           TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Значення за замовчуванням
INSERT INTO settings (key, value) VALUES
    ('logo_filename',  ''),
    ('bg_color',       '#F8F9FB'),
    ('primary_color',  '#1D6F42'),
    ('accent_color',   '#1D6F42'),
    ('text_color',     '#101828'),
    ('card_color',     '#FFFFFF'),
    ('border_color',   '#EAECF0'),
    ('font_family',    'Inter'),
    ('font_size',      '15'),
    ('page_title',     'VilnoPay'),
    ('page_subtitle',  'Безпечна оплата переказом'),
    ('footer_text',    'VilnoPayService · Захищено'),
    ('link_ttl_hours', '24'),
    ('custom_css',     '')
ON CONFLICT (key) DO NOTHING;

-- ── Отримувачі (рахунки для прийому платежів) ───────────────
CREATE TABLE IF NOT EXISTS receivers (
    id              SERIAL PRIMARY KEY,
    receiver_key    VARCHAR(50) UNIQUE NOT NULL,   -- rcv_abc123
    name            VARCHAR(200) NOT NULL,          -- назва для адмінки
    receiver        VARCHAR(200) NOT NULL,          -- ПІБ / назва юрособи
    iban            VARCHAR(34) NOT NULL,            -- UA + 27 цифр
    edrpou          VARCHAR(10) NOT NULL,            -- ЄДРПОУ / ІПН
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_receivers_key ON receivers(receiver_key);

-- ── API-ключі ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id              SERIAL PRIMARY KEY,
    key_hash        VARCHAR(128) NOT NULL,         -- SHA-256 хеш ключа
    key_prefix      VARCHAR(12) NOT NULL,          -- перші 8 символів для ідентифікації
    label           VARCHAR(100) NOT NULL DEFAULT 'default',
    is_active       BOOLEAN DEFAULT TRUE,
    last_used_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);

-- ── Лог згенерованих посилань (аудит) ───────────────────────
CREATE TABLE IF NOT EXISTS payment_links_log (
    id              SERIAL PRIMARY KEY,
    link_id         VARCHAR(32) NOT NULL,
    receiver_key    VARCHAR(50) NOT NULL,
    purpose         TEXT NOT NULL,
    amount          VARCHAR(20),
    api_key_prefix  VARCHAR(50),
    created_ip      VARCHAR(45),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_payment_links_created ON payment_links_log(created_at);
CREATE INDEX idx_payment_links_receiver ON payment_links_log(receiver_key);

-- ── Лог переглядів сторінок клієнтами ───────────────────────
CREATE TABLE IF NOT EXISTS page_views_log (
    id              SERIAL PRIMARY KEY,
    link_id         VARCHAR(32) NOT NULL,
    viewer_ip       VARCHAR(45),
    user_agent      TEXT,
    device_type     VARCHAR(20),           -- mobile / desktop / tablet
    bank_clicked    VARCHAR(30),           -- який банк обрали (NULL якщо не клікнули)
    viewed_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_page_views_link ON page_views_log(link_id);
CREATE INDEX idx_page_views_viewed ON page_views_log(viewed_at);

-- ── Тригер updated_at ───────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_admin_users_updated
    BEFORE UPDATE ON admin_users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_receivers_updated
    BEFORE UPDATE ON receivers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_settings_updated
    BEFORE UPDATE ON settings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
-- ── Менеджери (ролі) ────────────────────────────────────────
-- admin_users.role: 'admin' або 'manager'
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'admin';

-- ── Шаблони платежів для менеджерів ─────────────────────────
CREATE TABLE IF NOT EXISTS payment_templates (
    id              SERIAL PRIMARY KEY,
    manager_id      INTEGER REFERENCES admin_users(id) ON DELETE CASCADE,
    name            VARCHAR(100) NOT NULL,           -- назва шаблону
    receiver_key    VARCHAR(50) NOT NULL,             -- отримувач
    purpose         TEXT NOT NULL,                    -- призначення
    default_amount  VARCHAR(20),                     -- сума за замовчуванням (опціонально)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_templates_manager ON payment_templates(manager_id);


-- ── Платіжні провайдери (LiqPay, майбутні: MonoPay, Fondy) ──
CREATE TABLE IF NOT EXISTS payment_providers (
    id              SERIAL PRIMARY KEY,
    provider_type   VARCHAR(50) NOT NULL,         -- 'liqpay'
    name            VARCHAR(200) NOT NULL,         -- "LiqPay основний"
    is_active       BOOLEAN DEFAULT FALSE,
    public_key      TEXT NOT NULL DEFAULT '',
    private_key_enc TEXT NOT NULL DEFAULT '',       -- Fernet encrypted
    display_mode    VARCHAR(30) NOT NULL DEFAULT 'widget',
                    -- 'widget' | 'button' | 'redirect'
    pay_methods     TEXT DEFAULT '["card","privat24","wallet"]',
    is_sandbox      BOOLEAN DEFAULT FALSE,
    extra_config    TEXT DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_providers_type ON payment_providers(provider_type);
CREATE INDEX IF NOT EXISTS idx_providers_active ON payment_providers(is_active);

-- ── Транзакції LiqPay ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS liqpay_transactions (
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
);
CREATE INDEX IF NOT EXISTS idx_liqpay_tx_link ON liqpay_transactions(link_id);
CREATE INDEX IF NOT EXISTS idx_liqpay_tx_order ON liqpay_transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_liqpay_tx_status ON liqpay_transactions(status);

-- Тригери для нових таблиць
CREATE TRIGGER trg_payment_providers_updated
    BEFORE UPDATE ON payment_providers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_liqpay_tx_updated
    BEFORE UPDATE ON liqpay_transactions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Нова setting: порядок блоків
INSERT INTO settings (key, value) VALUES
    ('block_order', '["nbu_qr","liqpay","requisites"]')
ON CONFLICT (key) DO NOTHING;
