# Архітектурний план: інтеграція LiqPay у VilnoPayService

> Версія: 1.0 · Дата: 2025-06-29 · Автор: Архітектор · Замовник: Рома

---

## 1. Загальний огляд

### Що додаємо

Зараз на `/p/{link_id}`: QR-код НБУ + кнопка + реквізити + інвойс.

**Додаємо:**
- Блок LiqPay (віджет / кнопка / redirect — конфігурується)
- Таблицю `payment_providers` — розширювана абстракція
- Таблицю `liqpay_transactions` — лог транзакцій
- Setting `block_order` для порядку блоків
- `POST /liqpay/callback` — обробник callback
- `/liqpay/result/{link_id}` — сторінка результату оплати

### Принципи
1. Зворотна сумісність — без LiqPay все працює як раніше
2. Безпека — private_key зашифрований Fernet, callback верифікується
3. Конфігурація через адмінку — без редеплою
4. Розширюваність — готова для Mono Pay, Fondy тощо

---

## 2. Зміни в schema.sql

### 2.1 Таблиця `payment_providers`

```sql
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
CREATE INDEX idx_providers_type ON payment_providers(provider_type);
CREATE INDEX idx_providers_active ON payment_providers(is_active);
```

### 2.2 Таблиця `liqpay_transactions`

```sql
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
CREATE INDEX idx_liqpay_tx_link ON liqpay_transactions(link_id);
CREATE INDEX idx_liqpay_tx_order ON liqpay_transactions(order_id);
CREATE INDEX idx_liqpay_tx_status ON liqpay_transactions(status);
```

### 2.3 Нові settings + тригери

```sql
INSERT INTO settings (key, value) VALUES
    ('block_order', '["nbu_qr","liqpay","requisites"]')
ON CONFLICT (key) DO NOTHING;

CREATE TRIGGER trg_payment_providers_updated
    BEFORE UPDATE ON payment_providers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_liqpay_tx_updated
    BEFORE UPDATE ON liqpay_transactions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

---

## 3. Зміни в db.py

### 3.1 Шифрування private_key

Нова залежність: `cryptography` → додати в `requirements.txt`.

```python
from cryptography.fernet import Fernet

PROVIDER_ENCRYPTION_KEY = os.getenv("PROVIDER_ENCRYPTION_KEY", "")

def _get_fernet():
    if not PROVIDER_ENCRYPTION_KEY:
        raise ValueError("PROVIDER_ENCRYPTION_KEY не налаштований")
    return Fernet(PROVIDER_ENCRYPTION_KEY.encode())

def encrypt_private_key(plain_key: str) -> str:
    return _get_fernet().encrypt(plain_key.encode()).decode()

def decrypt_private_key(encrypted_key: str) -> str:
    return _get_fernet().decrypt(encrypted_key.encode()).decode()
```

Генерація ключа (один раз, зберегти в `.env`):
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3.2 CRUD payment_providers

```python
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

def get_active_providers():
    """Активні провайдери для pay page. БЕЗ private_key_enc."""
    return pg_query(
        """SELECT id,provider_type,name,public_key,display_mode,
           pay_methods,is_sandbox,extra_config
           FROM payment_providers WHERE is_active=TRUE ORDER BY id""",
        fetchall=True) or []

def list_providers():
    """Всі провайдери (адмінка). БЕЗ private_key_enc."""
    rows = pg_query(
        """SELECT id,provider_type,name,is_active,public_key,display_mode,
           pay_methods,is_sandbox,extra_config,created_at,updated_at
           FROM payment_providers ORDER BY created_at DESC""",
        fetchall=True) or []
    for r in rows: _serialize_provider(r)
    return rows

def update_provider(provider_id, **kwargs):
    allowed = {"name","is_active","public_key","display_mode",
               "pay_methods","is_sandbox","extra_config"}
    updates = {k:v for k,v in kwargs.items() if k in allowed}
    if "private_key" in kwargs and kwargs["private_key"]:
        updates["private_key_enc"] = encrypt_private_key(kwargs["private_key"])
    if not updates: return
    sets = ", ".join(f"{k}=%s" for k in updates)
    vals = list(updates.values()) + [provider_id]
    pg_execute(f"UPDATE payment_providers SET {sets} WHERE id=%s", vals)

def delete_provider(pid):
    pg_execute("DELETE FROM payment_providers WHERE id=%s", (pid,))

def get_provider_decrypted(provider_id):
    """З розшифрованим private_key — ТІЛЬКИ для формування підписів."""
    row = pg_query(
        "SELECT * FROM payment_providers WHERE id=%s AND is_active=TRUE",
        (provider_id,), fetchone=True)
    if row and row.get("private_key_enc"):
        row["private_key"] = decrypt_private_key(row["private_key_enc"])
    return row
```

### 3.3 CRUD liqpay_transactions

```python
def create_liqpay_tx(link_id, order_id, provider_id):
    pg_execute(
        "INSERT INTO liqpay_transactions (link_id,order_id,provider_id) VALUES (%s,%s,%s)",
        (link_id, order_id, provider_id))

def update_liqpay_tx(order_id, **kwargs):
    allowed = {"status","liqpay_order_id","amount","currency",
               "sender_card","transaction_id","callback_data","callback_ip"}
    updates = {k:v for k,v in kwargs.items() if k in allowed}
    if not updates: return
    sets = ", ".join(f"{k}=%s" for k in updates)
    vals = list(updates.values()) + [order_id]
    pg_execute(f"UPDATE liqpay_transactions SET {sets} WHERE order_id=%s", vals)

def get_liqpay_tx_by_link(link_id):
    row = pg_query(
        "SELECT * FROM liqpay_transactions WHERE link_id=%s ORDER BY created_at DESC LIMIT 1",
        (link_id,), fetchone=True)
    if row:
        for k in ("created_at","updated_at"):
            if row.get(k): row[k] = str(row[k])
    return row

def list_liqpay_transactions(limit=100):
    rows = pg_query(
        "SELECT * FROM liqpay_transactions ORDER BY created_at DESC LIMIT %s",
        (min(limit,500),), fetchall=True) or []
    for r in rows:
        for k in ("created_at","updated_at"):
            if r.get(k): r[k] = str(r[k])
    return rows
```

### 3.4 Додати в `_migrate()`

В масив `migrations` додати SQL-створення обох таблиць + індексів + setting `block_order` (з `IF NOT EXISTS` / `ON CONFLICT DO NOTHING`).

---

## 4. Зміни в app.py

### 4.1 Нові імпорти в app.py

```python
from db import (
    # ... існуючі ...
    create_provider, list_providers, update_provider, delete_provider,
    get_active_providers, get_provider_decrypted,
    create_liqpay_tx, update_liqpay_tx, get_liqpay_tx_by_link,
    list_liqpay_transactions
)
from templates import pay_page_html, expired_page_html, liqpay_result_html
```

### 4.2 LiqPay helpers

```python
def liqpay_encode_data(params: dict) -> str:
    """base64(JSON) для LiqPay API v3."""
    return base64.b64encode(json.dumps(params).encode()).decode()

def liqpay_signature(private_key: str, data: str) -> str:
    """signature = base64(sha1(private_key + data + private_key))
    УВАГА: LiqPay v3 використовує SHA-1, НЕ SHA-3!"""
    sign_str = private_key + data + private_key
    return base64.b64encode(hashlib.sha1(sign_str.encode()).digest()).decode()

def liqpay_verify_callback(private_key: str, data: str, signature: str) -> bool:
    return liqpay_signature(private_key, data) == signature
```

### 4.3 Endpoint: LiqPay checkout data (AJAX)

Сторінка оплати викликає цей endpoint для отримання data + signature.

```python
@app.get("/liqpay/checkout-data/{link_id}")
@limiter.limit("30/minute")
def liqpay_checkout_data(request: Request, link_id: str):
    link_id = _validate_link_id(link_id)
    raw = rdb.get(f"pay:{link_id}")
    if not raw:
        raise HTTPException(410, "Посилання неактивне")

    pay_data = json.loads(raw)
    amount = pay_data.get("amount")
    if not amount:
        raise HTTPException(400, "Сума не вказана — LiqPay потребує суму")

    # Знайти активного LiqPay провайдера
    providers = get_active_providers()
    lp = next((p for p in providers if p["provider_type"] == "liqpay"), None)
    if not lp:
        raise HTTPException(404, "LiqPay не налаштований")

    # Отримати private_key для підпису
    provider_full = get_provider_decrypted(lp["id"])
    if not provider_full or not provider_full.get("private_key"):
        raise HTTPException(500, "Ключі LiqPay не налаштовані")

    # Унікальний order_id
    order_id = f"vp_{link_id}_{secrets.token_urlsafe(6)}"

    # Зберегти транзакцію
    create_liqpay_tx(link_id, order_id, lp["id"])

    # Параметри LiqPay
    lp_params = {
        "version": 3,
        "public_key": lp["public_key"],
        "action": "pay",
        "amount": float(amount),
        "currency": "UAH",
        "description": pay_data.get("purpose", "Оплата"),
        "order_id": order_id,
        "server_url": f"{BASE_URL}/liqpay/callback",
        "result_url": f"{BASE_URL}/liqpay/result/{link_id}",
    }

    # Додати sandbox якщо потрібно
    if lp.get("is_sandbox"):
        lp_params["sandbox"] = 1

    # Додати дозволені методи оплати
    try:
        methods = json.loads(lp.get("pay_methods", "[]"))
        if methods:
            lp_params["paytypes"] = ",".join(methods)
    except (json.JSONDecodeError, TypeError):
        pass

    data_b64 = liqpay_encode_data(lp_params)
    signature = liqpay_signature(provider_full["private_key"], data_b64)

    return {
        "data": data_b64,
        "signature": signature,
        "public_key": lp["public_key"],
        "display_mode": lp["display_mode"],
        "is_sandbox": lp.get("is_sandbox", False)
    }
```

### 4.4 Callback handler

```python
@app.post("/liqpay/callback")
async def liqpay_callback(request: Request):
    """Server-to-server callback від LiqPay."""
    form = await request.form()
    data = form.get("data", "")
    signature = form.get("signature", "")

    if not data or not signature:
        logger.warning("LIQPAY_CALLBACK empty data/signature ip=%s", get_remote_address(request))
        raise HTTPException(400, "Missing data or signature")

    # Декодувати data для отримання order_id
    try:
        decoded = json.loads(base64.b64decode(data).decode())
    except Exception:
        logger.warning("LIQPAY_CALLBACK invalid data ip=%s", get_remote_address(request))
        raise HTTPException(400, "Invalid data")

    order_id = decoded.get("order_id", "")
    if not order_id:
        raise HTTPException(400, "Missing order_id")

    # Знайти провайдера через транзакцію
    from db import pg_query as _pq
    tx = _pq("SELECT provider_id FROM liqpay_transactions WHERE order_id=%s",
             (order_id,), fetchone=True)
    if not tx:
        logger.warning("LIQPAY_CALLBACK unknown order_id=%s", order_id)
        raise HTTPException(404, "Unknown order_id")

    provider = get_provider_decrypted(tx["provider_id"])
    if not provider:
        logger.error("LIQPAY_CALLBACK provider not found id=%s", tx["provider_id"])
        raise HTTPException(500, "Provider error")

    # Верифікація підпису
    if not liqpay_verify_callback(provider["private_key"], data, signature):
        logger.warning("LIQPAY_CALLBACK invalid signature order=%s ip=%s",
                       order_id, get_remote_address(request))
        raise HTTPException(403, "Invalid signature")

    # Оновити транзакцію
    status = decoded.get("status", "unknown")
    update_liqpay_tx(
        order_id,
        status=status,
        liqpay_order_id=decoded.get("liqpay_order_id"),
        amount=decoded.get("amount"),
        currency=decoded.get("currency", "UAH"),
        sender_card=decoded.get("sender_card_mask2", ""),
        transaction_id=decoded.get("transaction_id"),
        callback_data=json.dumps(decoded),
        callback_ip=get_remote_address(request)
    )

    logger.info("LIQPAY_CALLBACK order=%s status=%s amount=%s ip=%s",
                order_id, status, decoded.get("amount"), get_remote_address(request))

    # Якщо оплата успішна — зберегти в Redis для відображення
    if status in ("success", "sandbox"):
        link_id_part = order_id.split("_")[1] if "_" in order_id else ""
        if link_id_part:
            rdb.setex(f"liqpay_paid:{link_id_part}", 86400,
                      json.dumps({"status": status, "amount": decoded.get("amount")}))

    return {"ok": True}
```

### 4.5 Сторінка результату

```python
@app.get("/liqpay/result/{link_id}", response_class=HTMLResponse)
def liqpay_result(request: Request, link_id: str):
    """Сторінка після повернення з LiqPay (result_url)."""
    link_id = _validate_link_id(link_id)
    tx = get_liqpay_tx_by_link(link_id)
    settings = get_settings()
    logo_fn = settings.get("logo_filename", "")
    logo_url = f"/static/{logo_fn}" if logo_fn else ""
    return HTMLResponse(liqpay_result_html(tx, settings, logo_url))
```

### 4.6 Адмін-ендпоінти для провайдерів

```python
# ── Admin: Payment Providers ────────────────────────────────

@app.get("/admin/providers")
def admin_list_providers(request: Request):
    _require_role(request, "admin")
    return list_providers()

@app.post("/admin/providers")
def admin_create_provider(request: Request, body: dict):
    _require_role(request, "admin")
    required = ["provider_type", "name", "public_key", "private_key"]
    for f in required:
        if not body.get(f, "").strip():
            raise HTTPException(400, f"Поле {f} обов'язкове")
    prov = create_provider(
        provider_type=body["provider_type"].strip(),
        name=body["name"].strip(),
        public_key=body["public_key"].strip(),
        private_key=body["private_key"].strip(),
        display_mode=body.get("display_mode", "widget"),
        pay_methods=body.get("pay_methods", '["card","privat24","wallet"]'),
        is_sandbox=body.get("is_sandbox", False),
        extra_config=body.get("extra_config", "{}")
    )
    logger.info("PROVIDER_CREATED type=%s name=%s", body["provider_type"], body["name"])
    return prov or {"ok": True}

@app.put("/admin/providers/{provider_id}")
def admin_update_provider(request: Request, provider_id: int, body: dict):
    _require_role(request, "admin")
    update_provider(provider_id, **body)
    logger.info("PROVIDER_UPDATED id=%s", provider_id)
    return {"ok": True}

@app.delete("/admin/providers/{provider_id}")
def admin_delete_provider(request: Request, provider_id: int):
    _require_role(request, "admin")
    delete_provider(provider_id)
    logger.info("PROVIDER_DELETED id=%s", provider_id)
    return {"ok": True}

@app.get("/admin/liqpay-transactions")
def admin_liqpay_transactions(request: Request, limit: int = 100):
    _require_role(request, "admin")
    return list_liqpay_transactions(limit)
```

### 4.7 Зміни в CSP (SecurityHeadersMiddleware)

Для сторінок `/p/` додати LiqPay домени:

```python
# В SecurityHeadersMiddleware, гілка для публічних сторінок:
if request.url.path.startswith("/p/"):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "script-src 'self' 'unsafe-inline' https://static.liqpay.ua; "
        "frame-src https://www.liqpay.ua https://static.liqpay.ua; "
        "connect-src 'self' https://www.liqpay.ua; "
        "font-src 'self' https://fonts.gstatic.com"
    )
```

### 4.8 Зміни в `pay_page` endpoint

```python
@app.get("/p/{link_id}", response_class=HTMLResponse)
@limiter.limit("30/minute")
def pay_page(request: Request, link_id: str):
    # ... існуючий код ...

    # НОВЕ: отримати активних провайдерів і block_order
    active_providers = get_active_providers()
    block_order_raw = settings.get("block_order", '["nbu_qr","liqpay","requisites"]')
    try:
        block_order = json.loads(block_order_raw)
    except (json.JSONDecodeError, TypeError):
        block_order = ["nbu_qr", "liqpay", "requisites"]

    # Перевірити чи вже оплачено
    liqpay_paid = rdb.get(f"liqpay_paid:{link_id}")

    return HTMLResponse(content=pay_page_html(
        nbu_url, data["receiver"], data["iban"], data["purpose"],
        amt_line, qr_b64, hours_left, settings, logo_url, link_id,
        data.get("code", ""), ttl_seconds,
        data.get("invoice_url") or (f"{BASE_URL}/invoice/{link_id}" if data.get("invoice_id") else None),
        # НОВІ аргументи:
        providers=active_providers,
        block_order=block_order,
        liqpay_paid=json.loads(liqpay_paid) if liqpay_paid else None
    ))
```

---

## 5. Зміни в templates.py

### 5.1 Нова функція `liqpay_block_html()`

Генерує HTML для блоку LiqPay на сторінці оплати залежно від `display_mode`.

```python
def liqpay_block_html(link_id, provider, amount, liqpay_paid=None):
    """Генерує HTML-блок LiqPay для сторінки оплати.
    
    provider — dict з get_active_providers() (public_key, display_mode, pay_methods, ...)
    liqpay_paid — dict {"status":"success","amount":100} якщо вже оплачено
    """
    if liqpay_paid and liqpay_paid.get("status") in ("success", "sandbox"):
        return f'''<div class="section" style="background:#F0FAF4;border-color:#A9D6B8">
<div style="text-align:center;padding:16px 0">
<div style="font-size:48px;margin-bottom:8px">✅</div>
<div style="font-size:18px;font-weight:700;color:#1D6F42">Оплата пройшла успішно</div>
<div style="font-size:14px;color:#667085;margin-top:4px">Сума: {_e(str(liqpay_paid.get("amount","")))} грн</div>
</div></div>'''

    if not amount:
        return '''<div class="section">
<div style="text-align:center;padding:12px 0;color:var(--muted);font-size:13px">
Оплата LiqPay недоступна: сума не вказана</div></div>'''

    mode = provider.get("display_mode", "widget")
    lid = _e(link_id)

    if mode == "widget":
        # JS-віджет LiqPay — вбудовується на сторінку
        return f'''<div class="section" id="liqpay-section">
<div class="sec-label">Оплата картою</div>
<div id="liqpay-widget-container" style="min-height:200px;display:flex;align-items:center;justify-content:center">
<div style="color:var(--muted);font-size:13px">Завантаження...</div>
</div>
<script src="https://static.liqpay.ua/libjs/checkout.js"></script>
<script>
(function(){{
  fetch('/liqpay/checkout-data/{lid}')
    .then(r=>r.json())
    .then(d=>{{
      if(d.data && d.signature){{
        LiqPayCheckout.init({{
          data: d.data,
          signature: d.signature,
          embedTo: "#liqpay-widget-container",
          mode: "embed"
        }}).on("liqpay.callback", function(data){{
          if(data.status==="success"||data.status==="sandbox"){{
            document.getElementById('liqpay-section').innerHTML=
              '<div style="text-align:center;padding:20px"><div style="font-size:48px">✅</div>'
              +'<div style="font-size:18px;font-weight:700;color:#1D6F42;margin-top:8px">Оплата успішна!</div></div>';
          }}
        }});
      }} else {{
        document.getElementById('liqpay-widget-container').innerHTML=
          '<div style="color:#D92D20;font-size:13px">Не вдалося завантажити форму оплати</div>';
      }}
    }})
    .catch(()=>{{
      document.getElementById('liqpay-widget-container').innerHTML=
        '<div style="color:#D92D20;font-size:13px">Помилка завантаження</div>';
    }});
}})();
</script>
</div>'''

    elif mode == "button":
        # Кнопка — при кліку робить POST-форму на LiqPay checkout
        return f'''<div class="section" id="liqpay-section">
<div class="sec-label">Оплата картою</div>
<button class="pay-btn" id="liqpay-pay-btn" onclick="liqpayPay()" style="background:#7ab72b">
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>
Оплатити через LiqPay
</button>
<script>
function liqpayPay(){{
  var btn=document.getElementById('liqpay-pay-btn');
  btn.disabled=true; btn.textContent='Завантаження...';
  fetch('/liqpay/checkout-data/{lid}')
    .then(r=>r.json())
    .then(d=>{{
      if(!d.data||!d.signature)throw new Error('no data');
      var f=document.createElement('form');
      f.method='POST'; f.action='https://www.liqpay.ua/api/3/checkout'; f.target='_blank';
      var i1=document.createElement('input'); i1.type='hidden'; i1.name='data'; i1.value=d.data;
      var i2=document.createElement('input'); i2.type='hidden'; i2.name='signature'; i2.value=d.signature;
      f.appendChild(i1); f.appendChild(i2); document.body.appendChild(f); f.submit();
      btn.disabled=false; btn.textContent='Оплатити через LiqPay';
    }})
    .catch(()=>{{ btn.disabled=false; btn.textContent='Оплатити через LiqPay'; showToast('Помилка'); }});
}}
</script>
</div>'''

    elif mode == "redirect":
        # Автоматичний POST на LiqPay при натисканні
        return f'''<div class="section" id="liqpay-section">
<div class="sec-label">Оплата картою</div>
<form id="liqpay-form" method="POST" action="https://www.liqpay.ua/api/3/checkout">
<input type="hidden" name="data" id="liqpay-data">
<input type="hidden" name="signature" id="liqpay-sig">
<button type="submit" class="pay-btn" style="background:#7ab72b" disabled>Завантаження...</button>
</form>
<script>
fetch('/liqpay/checkout-data/{lid}')
  .then(r=>r.json())
  .then(d=>{{
    document.getElementById('liqpay-data').value=d.data;
    document.getElementById('liqpay-sig').value=d.signature;
    var btn=document.querySelector('#liqpay-form button');
    btn.disabled=false; btn.textContent='Оплатити через LiqPay →';
  }})
  .catch(()=>{{
    document.querySelector('#liqpay-form button').textContent='Помилка завантаження';
  }});
</script>
</div>'''

    return ""
```

### 5.2 Функція `liqpay_result_html()`

```python
def liqpay_result_html(tx, settings=None, logo_url=""):
    """Сторінка результату після повернення з LiqPay."""
    s = settings or {}
    bg, pc, ac, tc, cc_color, bc, ff, fs, cc = _css_vars(s)
    pt = _e(s.get("page_title", "VilnoPay"))
    lu = _e(logo_url) if logo_url else ""
    logo = f'<img src="{lu}" alt="Logo" style="max-height:44px;margin-bottom:8px;border-radius:8px;">' if lu else ""

    if tx and tx.get("status") in ("success", "sandbox"):
        icon = "✅"
        title = "Оплата успішна"
        desc = f"Сума: {tx.get('amount', '—')} {tx.get('currency', 'UAH')}"
        color = "#1D6F42"
    elif tx and tx.get("status") == "failure":
        icon = "❌"
        title = "Оплата не пройшла"
        desc = "Спробуйте ще раз або використайте інший спосіб оплати"
        color = "#D92D20"
    else:
        icon = "⏳"
        title = "Обробка оплати"
        desc = "Зачекайте — статус оновиться автоматично"
        color = "#F59E0B"

    return f"""<!DOCTYPE html>
<html lang="uk"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{pt} — Результат оплати</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
body{{font-family:'Inter',sans-serif;background:{bg};display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
.card{{background:{cc_color};border-radius:16px;padding:40px 32px;max-width:400px;width:100%;text-align:center;box-shadow:0 4px 16px rgba(0,0,0,.06);border:1px solid {bc}}}
.icon{{font-size:56px;margin-bottom:16px}}
h2{{font-size:22px;font-weight:700;color:{color};margin-bottom:10px}}
p{{font-size:14px;color:#667085;line-height:1.5}}
{cc}
</style></head><body>
<div class="card">{logo}<div class="icon">{icon}</div>
<h2>{title}</h2><p>{desc}</p></div></body></html>"""
```

### 5.3 Зміни в `pay_page_html()`

Оновити сигнатуру:

```python
def pay_page_html(nbu_url, receiver, iban, purpose, amount_line, qr_b64,
                  hours_left, settings=None, logo_url="", link_id="",
                  code="", ttl_seconds=0, invoice_url=None,
                  providers=None, block_order=None, liqpay_paid=None):
```

Замість хардкоженої послідовності блоків — динамічний рендеринг:

```python
    # Порядок блоків
    if block_order is None:
        block_order = ["nbu_qr", "liqpay", "requisites"]

    blocks_html = ""
    for block_name in block_order:
        if block_name == "nbu_qr":
            blocks_html += NBU_QR_BLOCK  # існуючий HTML QR + кнопка банку
        elif block_name == "liqpay":
            lp = next((p for p in (providers or []) if p["provider_type"] == "liqpay"), None)
            if lp:
                amount_raw = amount_line.replace(" грн", "").replace("за домовленістю", "").strip()
                blocks_html += liqpay_block_html(link_id, lp, amount_raw, liqpay_paid)
        elif block_name == "requisites":
            blocks_html += REQUISITES_BLOCK  # існуючий HTML реквізитів

    # Вставити blocks_html у шаблон замість хардкодженого порядку
```

Конкретніше — розбити існуючий HTML у `pay_page_html()` на іменовані блоки:

```python
    # --- Блок NBU QR ---
    nbu_qr_block = f'''
    <div class="section">
    <a class="pay-btn" href="{nbu}" target="_blank" rel="noopener">
    {BANK_ICON} Оплатити через додаток банку</a></div>
    <div class="section">
    <div class="qr-wrap">
    <img class="qr-img" id="qr-image" src="data:image/png;base64,{qr_b64}" ...>
    ...
    </div></div>'''

    # --- Блок LiqPay ---
    lp = next((p for p in (providers or []) if p["provider_type"] == "liqpay"), None)
    liqpay_block = liqpay_block_html(link_id, lp, amt_raw, liqpay_paid) if lp else ""

    # --- Блок Реквізити ---
    requisites_block = f'''
    <div class="section">
    <div class="sec-label">Реквізити для переказу</div>
    {reqs}
    <button class="copy-all" onclick="copyAll(this)">Скопіювати всі реквізити</button>
    </div>'''

    # --- Збірка за порядком ---
    block_map = {
        "nbu_qr": nbu_qr_block,
        "liqpay": liqpay_block,
        "requisites": requisites_block,
    }
    blocks_html = "\n".join(block_map.get(b, "") for b in block_order)
```

### 5.4 CSS для LiqPay блоку

Додати в `<style>` сторінки оплати:

```css
/* LiqPay widget container */
#liqpay-widget-container iframe {
    border-radius: var(--rs) !important;
    border: 1px solid var(--border) !important;
    max-width: 100% !important;
}
#liqpay-section .sec-label {
    display: flex;
    align-items: center;
    gap: 6px;
}
#liqpay-section .sec-label::before {
    content: "💳";
}
```

---

## 6. Зміни в admin.html

### 6.1 Нова вкладка "Провайдери"

В `<div class="tabs">` додати:

```html
<div class="tab" data-t="prov">Провайдери</div>
```

### 6.2 Панель провайдерів

```html
<div class="panel" id="p-prov">
<div class="card">
<h2>Платіжні провайдери</h2>
<div id="prov-tbl"></div>
<button class="btn btn-blue mt" onclick="showProvForm()">+ Додати провайдера</button>
</div>

<div class="card" id="prov-form" style="display:none">
<h2 id="prov-form-t">Новий провайдер</h2>
<input type="hidden" id="prov-eid">

<div class="fg"><label>Тип</label>
<select id="prov-type">
  <option value="liqpay">LiqPay</option>
</select></div>

<div class="fg"><label>Назва</label>
<input id="prov-name" placeholder="LiqPay основний"></div>

<div class="fg"><label>Public Key</label>
<input id="prov-pub" placeholder="sandbox_i000000000" class="mono"></div>

<div class="fg"><label>Private Key</label>
<input id="prov-priv" type="password" placeholder="sandbox_xxxxxxxxxxxxxxxx" class="mono">
<div style="font-size:11px;color:var(--muted);margin-top:4px">
⚠️ Зберігається зашифрованим (AES). При редагуванні — залиште порожнім щоб не змінювати.
</div></div>

<div class="fg"><label>Режим відображення</label>
<select id="prov-mode">
  <option value="widget">Віджет (вбудований на сторінку)</option>
  <option value="button">Кнопка (відкриє LiqPay в новому вікні)</option>
  <option value="redirect">Redirect (POST-форма на LiqPay)</option>
</select></div>

<div class="fg"><label>Методи оплати (JSON array)</label>
<input id="prov-methods" value='["card","privat24","wallet"]' class="mono"></div>

<div class="fg"><label>
<input type="checkbox" id="prov-sandbox"> Sandbox (тестовий режим)
</label></div>

<div class="err" id="prov-err"></div>
<div class="flex mt">
<button class="btn btn-green" onclick="saveProv()">Зберегти</button>
<button class="btn btn-outline" onclick="hideProvForm()">Скасувати</button>
</div>
</div>

<!-- Лог транзакцій LiqPay -->
<div class="card">
<h2>Транзакції LiqPay</h2>
<div id="liqpay-tx-tbl"></div>
</div>
</div>
```

### 6.3 JavaScript для провайдерів

```javascript
// PROVIDERS
async function loadProviders(){
  try{
    const rows=await api('GET','/admin/providers');
    const el=E('prov-tbl');
    if(!rows.length){
      el.innerHTML='<p style="color:var(--muted)">Немає провайдерів</p>';
      return;
    }
    let h='<div class="resp-table"><table><thead><tr>';
    h+='<th>Тип</th><th>Назва</th><th>Public Key</th><th>Режим</th><th>Статус</th><th></th>';
    h+='</tr></thead><tbody>';
    for(const r of rows){
      const b=r.is_active
        ?'<span class="badge badge-g">Активний</span>'
        :'<span class="badge badge-r">Вимкнено</span>';
      const sb=r.is_sandbox?'<span class="badge" style="background:#FFF7ED;color:#C2410C">Sandbox</span>':'';
      h+=`<tr>
        <td>${esc(r.provider_type)}</td>
        <td>${esc(r.name)}</td>
        <td class="mono">${esc((r.public_key||'').substring(0,20))}...</td>
        <td>${esc(r.display_mode)}</td>
        <td>${b} ${sb}</td>
        <td>
          <button class="btn btn-outline btn-sm" onclick="editProv(${r.id})">✏️</button>
          <button class="btn btn-outline btn-sm" onclick="toggleProv(${r.id},${!r.is_active})">${r.is_active?'Вимкнути':'Увімкнути'}</button>
          <button class="btn btn-red btn-sm" onclick="delProv(${r.id})">🗑</button>
        </td>
      </tr>`;
    }
    h+='</tbody></table></div>';
    el.innerHTML=h;
  }catch(e){toast('Помилка: '+e.message);}
}

function showProvForm(){
  E('prov-form').style.display='block';
  E('prov-form-t').textContent='Новий провайдер';
  E('prov-eid').value='';
  E('prov-type').value='liqpay';
  E('prov-name').value='';
  E('prov-pub').value='';
  E('prov-priv').value='';
  E('prov-mode').value='widget';
  E('prov-methods').value='["card","privat24","wallet"]';
  E('prov-sandbox').checked=false;
  E('prov-err').textContent='';
}
function hideProvForm(){E('prov-form').style.display='none';}

async function saveProv(){
  const eid=E('prov-eid').value;
  const body={
    provider_type: E('prov-type').value,
    name: E('prov-name').value.trim(),
    public_key: E('prov-pub').value.trim(),
    private_key: E('prov-priv').value.trim(),
    display_mode: E('prov-mode').value,
    pay_methods: E('prov-methods').value.trim(),
    is_sandbox: E('prov-sandbox').checked
  };
  try{
    E('prov-err').textContent='';
    if(eid){
      if(!body.private_key) delete body.private_key; // не змінювати якщо порожнє
      await api('PUT','/admin/providers/'+eid,body);
      toast('Оновлено');
    }else{
      if(!body.private_key){E('prov-err').textContent='Private key обов\'язковий';return;}
      await api('POST','/admin/providers',body);
      toast('Створено');
    }
    hideProvForm(); loadProviders();
  }catch(e){E('prov-err').textContent=e.message;}
}

async function editProv(id){
  const rows=await api('GET','/admin/providers');
  const r=rows.find(x=>x.id===id);
  if(!r)return;
  E('prov-form').style.display='block';
  E('prov-form-t').textContent='Редагувати: '+r.name;
  E('prov-eid').value=id;
  E('prov-type').value=r.provider_type;
  E('prov-name').value=r.name;
  E('prov-pub').value=r.public_key;
  E('prov-priv').value=''; // не показуємо
  E('prov-mode').value=r.display_mode;
  E('prov-methods').value=r.pay_methods||'[]';
  E('prov-sandbox').checked=r.is_sandbox;
}

async function toggleProv(id,active){
  try{
    await api('PUT','/admin/providers/'+id,{is_active:active});
    toast(active?'Увімкнено':'Вимкнено');
    loadProviders();
  }catch(e){toast('Помилка: '+e.message);}
}

async function delProv(id){
  if(!confirm('Видалити провайдера?'))return;
  try{await api('DELETE','/admin/providers/'+id);toast('Видалено');loadProviders();}
  catch(e){toast('Помилка: '+e.message);}
}

async function loadLiqpayTx(){
  try{
    const rows=await api('GET','/admin/liqpay-transactions');
    const el=E('liqpay-tx-tbl');
    if(!rows.length){el.innerHTML='<p style="color:var(--muted)">Немає транзакцій</p>';return;}
    let h='<div class="resp-table"><table><thead><tr>';
    h+='<th>Order ID</th><th>Link ID</th><th>Статус</th><th>Сума</th><th>Картка</th><th>Час</th>';
    h+='</tr></thead><tbody>';
    for(const r of rows){
      const sc = r.status==='success'?'badge-g':(r.status==='failure'?'badge-r':'');
      h+=`<tr>
        <td class="mono">${esc(r.order_id)}</td>
        <td class="mono">${esc(r.link_id)}</td>
        <td><span class="badge ${sc}">${esc(r.status||'pending')}</span></td>
        <td>${r.amount||'—'} ${esc(r.currency||'')}</td>
        <td class="mono">${esc(r.sender_card||'—')}</td>
        <td>${fmtKiev(r.created_at)}</td>
      </tr>`;
    }
    h+='</tbody></table></div>';el.innerHTML=h;
  }catch(e){}
}
```

### 6.4 Блок порядку блоків в Брендингу

Додати в панель `p-brand`:

```html
<div class="fg"><label>Порядок блоків на сторінці оплати</label>
<div id="block-order-editor" style="display:flex;flex-direction:column;gap:6px">
</div>
<div style="font-size:11px;color:var(--muted);margin-top:4px">
Перетягніть для зміни порядку. Доступні: nbu_qr, liqpay, requisites
</div>
</div>
```

JavaScript для drag-and-drop порядку:

```javascript
function renderBlockOrder(order){
  const el=E('block-order-editor');
  const labels={'nbu_qr':'🏦 QR НБУ + кнопка банку','liqpay':'💳 LiqPay','requisites':'📋 Реквізити'};
  el.innerHTML='';
  order.forEach((b,i)=>{
    const d=document.createElement('div');
    d.style.cssText='display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--card);cursor:move';
    d.draggable=true;
    d.dataset.block=b;
    d.innerHTML=`<span style="color:var(--muted)">${i+1}.</span> ${labels[b]||b}
      <span style="margin-left:auto;cursor:pointer;color:var(--muted)" onclick="moveBlock('${b}',-1)">↑</span>
      <span style="cursor:pointer;color:var(--muted)" onclick="moveBlock('${b}',1)">↓</span>`;
    el.appendChild(d);
  });
}
function moveBlock(name,dir){
  let order=getBlockOrder();
  const i=order.indexOf(name);
  if(i<0)return;
  const ni=i+dir;
  if(ni<0||ni>=order.length)return;
  [order[i],order[ni]]=[order[ni],order[i]];
  E('s-block_order').value=JSON.stringify(order);
  renderBlockOrder(order);
}
function getBlockOrder(){
  try{return JSON.parse(E('s-block_order').value);}
  catch(e){return ["nbu_qr","liqpay","requisites"];}
}
```

### 6.5 Оновити `loadAll()`

```javascript
function loadAll(){loadRcv();loadBrand();loadKeys();loadLog();loadViews();loadManagers();loadProviders();loadLiqpayTx();}
```

Додати hidden input для `block_order` в брендинг і включити його в `saveBrand()`:

```html
<input type="hidden" id="s-block_order" value='["nbu_qr","liqpay","requisites"]'>
```

Оновити `allowed` set в admin_update_settings:
```python
allowed = {"logo_filename","bg_color","primary_color","accent_color",
           "text_color","card_color","border_color","font_family","font_size",
           "page_title","page_subtitle","footer_text","link_ttl_hours","custom_css",
           "block_order"}  # НОВЕ
```

---

## 7. Зміни в settings

### 7.1 Нові ключі в settings

| Ключ | Значення за замовчуванням | Опис |
|------|--------------------------|------|
| `block_order` | `["nbu_qr","liqpay","requisites"]` | Порядок блоків на сторінці оплати |

### 7.2 Валідація block_order

В `admin_update_settings` (app.py):

```python
if "block_order" in filtered:
    try:
        order = json.loads(filtered["block_order"])
        valid_blocks = {"nbu_qr", "liqpay", "requisites"}
        if not isinstance(order, list) or not all(b in valid_blocks for b in order):
            raise HTTPException(400, "block_order: невірний формат")
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(400, "block_order: невалідний JSON")
```

---

## 8. Безпека

### 8.1 Зберігання ключів

| Ключ | Де зберігається | Як |
|------|-----------------|-----|
| `public_key` | PostgreSQL `payment_providers.public_key` | Plain text (не секрет) |
| `private_key` | PostgreSQL `payment_providers.private_key_enc` | Fernet encrypted |
| `PROVIDER_ENCRYPTION_KEY` | `.env` файл | Base64, 32 байти |

**Fernet** забезпечує:
- AES-128-CBC шифрування
- HMAC-SHA256 автентифікація (tamper protection)
- Timestamp (можна контролювати TTL токена)

### 8.2 Callback верифікація

```
1. LiqPay POST → /liqpay/callback з data + signature
2. Ми декодуємо data → отримуємо order_id
3. По order_id знаходимо provider_id → розшифровуємо private_key
4. Рахуємо expected_signature = base64(sha1(private_key + data + private_key))
5. Порівнюємо з отриманою signature
6. Тільки якщо збігається — оновлюємо транзакцію
```

### 8.3 IP whitelist для callback (опціонально)

LiqPay callback приходить з фіксованих IP. Можна додати перевірку:

```python
LIQPAY_CALLBACK_IPS = {
    "185.67.232.0/22",  # Приватбанк/LiqPay діапазон
}
# Або не обмежувати — signature verification достатньо
```

**Рекомендація:** покладатися на signature verification. IP можуть змінитися.

### 8.4 CSP (Content Security Policy)

Для сторінки `/p/{link_id}` оновити CSP:

```
script-src 'self' 'unsafe-inline' https://static.liqpay.ua;
frame-src https://www.liqpay.ua https://static.liqpay.ua;
connect-src 'self' https://www.liqpay.ua;
```

### 8.5 Rate limiting

- `/liqpay/checkout-data/{link_id}` — 30/хв (вже вказано)
- `/liqpay/callback` — НЕ лімітувати (server-to-server від LiqPay)
- `/liqpay/result/{link_id}` — 30/хв

### 8.6 Private key НІКОЛИ не потрапляє:
- ❌ В response API
- ❌ В логи
- ❌ На фронтенд
- ❌ В Redis
- ✅ Тільки для обчислення signature в пам'яті → одразу видаляється

---

## 9. Docker / Інфраструктура

### 9.1 Зміни в `.env`

```env
# Існуючі...
PROVIDER_ENCRYPTION_KEY=<згенерований-fernet-key>
```

### 9.2 Зміни в `requirements.txt`

Додати:
```
cryptography>=42.0
```

### 9.3 Зміни в `Dockerfile`

Не потрібні — `cryptography` встановлюється через pip.

### 9.4 Зміни в `docker-compose.yml`

Не потрібні — `PROVIDER_ENCRYPTION_KEY` підхоплюється через `env_file: .env`.

### 9.5 Опціонально: Docker secrets

Для production можна замінити env на Docker secret:

```yaml
services:
  pay-service:
    secrets:
      - provider_encryption_key
    environment:
      PROVIDER_ENCRYPTION_KEY_FILE: /run/secrets/provider_encryption_key

secrets:
  provider_encryption_key:
    file: ./secrets/provider_encryption_key.txt
```

І в db.py:
```python
def _read_secret(env_key):
    file_path = os.getenv(f"{env_key}_FILE")
    if file_path and Path(file_path).exists():
        return Path(file_path).read_text().strip()
    return os.getenv(env_key, "")

PROVIDER_ENCRYPTION_KEY = _read_secret("PROVIDER_ENCRYPTION_KEY")
```

---

## 10. Порядок реалізації

### Фаза 1: Backend (без UI) — ~2-3 год

1. ✅ `requirements.txt` → додати `cryptography`
2. ✅ `schema.sql` → додати таблиці + settings
3. ✅ `db.py` → encryption + CRUD providers + CRUD transactions + _migrate()
4. ✅ `app.py` → LiqPay helpers + endpoints (checkout-data, callback, result, admin CRUD)
5. ✅ `.env` → PROVIDER_ENCRYPTION_KEY
6. ✅ Тест: створити провайдера через curl, перевірити encryption/decryption

### Фаза 2: Frontend (templates) — ~2-3 год

7. ✅ `templates.py` → `liqpay_block_html()` + `liqpay_result_html()` + block_order рендеринг
8. ✅ Модифікація `pay_page_html()` → динамічний порядок блоків
9. ✅ CSP оновлення для LiqPay

### Фаза 3: Адмінка — ~2-3 год

10. ✅ `admin.html` → вкладка "Провайдери" + форма + таблиця
11. ✅ `admin.html` → block order editor в Брендингу
12. ✅ `app.py` → block_order валідація в settings update

### Фаза 4: Тестування — ~1-2 год

13. ✅ Sandbox тест LiqPay (sandbox ключі)
14. ✅ Callback тест (ngrok або прямий IP)
15. ✅ Тест різних display_mode (widget, button, redirect)
16. ✅ Тест block_order (зміна порядку через адмінку)
17. ✅ Тест без LiqPay (зворотна сумісність)

**Загальний час: ~8-11 годин.**

---

## 11. Діаграма потоку

### Оплата через LiqPay (widget mode)

```
Клієнт                  VilnoPayService              LiqPay
  │                          │                          │
  │  GET /p/{link_id}        │                          │
  │─────────────────────────>│                          │
  │  HTML (з LiqPay widget)  │                          │
  │<─────────────────────────│                          │
  │                          │                          │
  │  fetch /liqpay/          │                          │
  │  checkout-data/{link_id} │                          │
  │─────────────────────────>│                          │
  │                          │  create_liqpay_tx()      │
  │                          │  generate data+signature │
  │  {data, signature}       │                          │
  │<─────────────────────────│                          │
  │                          │                          │
  │  LiqPayCheckout.init()   │                          │
  │─────────────────────────────────────────────────────>│
  │  (iframe widget)         │                          │
  │<─────────────────────────────────────────────────────│
  │                          │                          │
  │  Клієнт оплачує          │                          │
  │─────────────────────────────────────────────────────>│
  │                          │                          │
  │                          │  POST /liqpay/callback   │
  │                          │<─────────────────────────│
  │                          │  verify signature        │
  │                          │  update_liqpay_tx()      │
  │                          │  set liqpay_paid:{}      │
  │                          │  {"ok": true}            │
  │                          │─────────────────────────>│
  │                          │                          │
  │  JS callback: success    │                          │
  │<─────────────────────────────────────────────────────│
  │  Показати ✅              │                          │
```

### Оплата через LiqPay (button/redirect mode)

```
Клієнт                  VilnoPayService              LiqPay
  │                          │                          │
  │  Клік "Оплатити LiqPay"  │                          │
  │  fetch checkout-data     │                          │
  │─────────────────────────>│                          │
  │  {data, signature}       │                          │
  │<─────────────────────────│                          │
  │                          │                          │
  │  POST form → liqpay.ua   │                          │
  │─────────────────────────────────────────────────────>│
  │  Checkout page           │                          │
  │<─────────────────────────────────────────────────────│
  │                          │                          │
  │  Оплата                  │                          │
  │─────────────────────────────────────────────────────>│
  │                          │  POST /liqpay/callback   │
  │                          │<─────────────────────────│
  │                          │  verify + update         │
  │                          │─────────────────────────>│
  │                          │                          │
  │  Redirect result_url     │                          │
  │─────────────────────────────────────────────────────>│
  │  GET /liqpay/result/     │                          │
  │  {link_id}               │                          │
  │─────────────────────────>│                          │
  │  HTML (✅ або ❌)         │                          │
  │<─────────────────────────│                          │
```

---

## 12. Файлова структура змін

```
VilnoPayService/
├── schema.sql          + payment_providers, liqpay_transactions, block_order setting
├── db.py               + encrypt/decrypt, CRUD providers, CRUD transactions, _migrate
├── app.py              + liqpay helpers, 6 нових endpoints, CSP, pay_page зміни
├── templates.py        + liqpay_block_html(), liqpay_result_html(), block_order в pay_page
├── admin.html          + вкладка Провайдери, block order editor
├── manager.html        (без змін)
├── requirements.txt    + cryptography>=42.0
├── .env                + PROVIDER_ENCRYPTION_KEY
├── docker-compose.yml  (без змін)
├── Dockerfile          (без змін)
└── ARCHITECTURE_LIQPAY.md  ← цей файл
```

---

## 13. Обмеження та рішення

| Обмеження | Рішення |
|-----------|---------|
| LiqPay потребує суму — деякі посилання без суми | Блок LiqPay показує "недоступно: сума не вказана" |
| Callback може прийти до того як клієнт побачить result_url | Redis `liqpay_paid:{link_id}` + перевірка в pay_page |
| LiqPay SHA-1 (не SHA-3/256 як в деякій документації) | Використовуємо SHA-1 — перевірено з офіційним SDK |
| Sandbox ключі відрізняються від production | Поле `is_sandbox` в providers — різні ключі |
| Container read-only filesystem | Ніяких файлових операцій для LiqPay — все в БД |

---

## 14. Тестування (sandbox)

### Sandbox ключі LiqPay

```
public_key:  sandbox_i000000000
private_key: sandbox_xxxxxxxxxxxxxxxx
```

Отримати на: https://www.liqpay.ua/dashboard → Sandbox

### Тестова картка

```
Номер: 4242 4242 4242 4242
Expiry: 12/29
CVV: 123
```

### Callback тестування

Для локальної розробки використати ngrok:
```bash
ngrok http 8000
# BASE_URL=https://xxxx.ngrok.io
```

---

*Кінець архітектурного плану.*
