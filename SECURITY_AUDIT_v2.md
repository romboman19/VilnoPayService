# 🔒 VilnoPayService — Аудит безпеки нової архітектури

**Дата:** 2026-06-24  
**Аудитор:** AI Security Auditor (subagent vp-security-v2)  
**Версія коду:** 3.0.0 (поточна) + рекомендації для v4.0 (адмінка + receiver_key)  
**Стек:** FastAPI + Redis + (плановано: PostgreSQL + Admin panel)  

---

## Короткий підсумок

| Категорія | Статус в v3.0 | Ризик для нової архітектури |
|-----------|--------------|----------------------------|
| XSS | ✅ Виправлено | 🔴 Новий ризик через адмінку (брендинг) |
| API key security | ✅ Header-based | 🟡 Потрібна ротація + зберігання |
| Rate limiting | ✅ slowapi + nginx | 🟠 Адмін-ендпоінти не покриті |
| receiver_key ентропія | — | ✅ Рекомендація нижче |
| Session / JWT | — | 🔴 Поки не реалізовано |
| CSRF | — | 🔴 Поки не реалізовано |
| Brute-force auth | — | 🔴 Поки не реалізовано |
| File upload | — | 🔴 Поки не реалізовано |
| PostgreSQL | — | 🟡 Потрібні налаштування |
| SQL Injection (Prisma/psycopg) | — | ✅ За замовчуванням безпечно (з умовами) |

---

## 1. Адмін-панель: Auth, CSRF, Brute-force

### 1.1 Session vs JWT — що обрати

**Рекомендація: HttpOnly Cookie + Server-side session (Redis).**

Причини:
- JWT у localStorage — вразливий до XSS (токен крадуть через `localStorage.getItem`)
- JWT у cookie — потребує CSRF захисту (але це реалізуємо)
- Server-side session — можна інвалідувати миттєво (logout справжній)
- Redis вже є в стеку

#### Правильна реалізація (FastAPI + Redis sessions):

```python
import secrets, json, bcrypt
from fastapi import FastAPI, Request, Response, Depends, HTTPException, Form
from starlette.middleware.base import BaseHTTPMiddleware

ADMIN_SESSION_TTL = 3600  # 1 година
ADMIN_COOKIE_NAME = "admin_session"

def create_admin_session(rdb, admin_id: str, ip: str) -> str:
    """Створює нову сесію, повертає session_id"""
    session_id = secrets.token_urlsafe(32)  # 256 біт ентропії
    rdb.setex(
        f"admin_session:{session_id}",
        ADMIN_SESSION_TTL,
        json.dumps({
            "admin_id": admin_id,
            "ip": ip,
            "created_at": int(time.time())
        })
    )
    return session_id

def get_admin_session(rdb, session_id: str) -> dict | None:
    raw = rdb.get(f"admin_session:{session_id}")
    if not raw:
        return None
    # Sliding window: продовжуємо TTL при кожному запиті
    rdb.expire(f"admin_session:{session_id}", ADMIN_SESSION_TTL)
    return json.loads(raw)

def destroy_admin_session(rdb, session_id: str):
    rdb.delete(f"admin_session:{session_id}")

def set_session_cookie(response: Response, session_id: str):
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=session_id,
        httponly=True,      # КРИТИЧНО: JS не може читати
        secure=True,        # КРИТИЧНО: тільки HTTPS
        samesite="strict",  # CSRF захист
        max_age=ADMIN_SESSION_TTL,
        path="/admin"       # Обмежити scope cookie
    )

async def require_admin(request: Request) -> dict:
    session_id = request.cookies.get(ADMIN_COOKIE_NAME)
    if not session_id:
        raise HTTPException(401, "Необхідна авторизація")
    session = get_admin_session(request.app.state.rdb, session_id)
    if not session:
        raise HTTPException(401, "Сесія закінчилась")
    return session

@app.post("/admin/login")
@limiter.limit("5/minute")
async def admin_login(request: Request, response: Response,
                      username: str = Form(), password: str = Form()):
    ip = get_remote_address(request)
    stored_hash = get_admin_password_hash(username)  # з .env або БД
    if not stored_hash:
        # Завжди однаковий час відповіді (timing attack prevention)
        bcrypt.checkpw(b"dummy", b"$2b$12$aaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        raise HTTPException(401, "Невірні дані")
    if not bcrypt.checkpw(password.encode(), stored_hash):
        raise HTTPException(401, "Невірні дані")
    
    # Знищити стару сесію перед створенням нової (session fixation!)
    old_sid = request.cookies.get(ADMIN_COOKIE_NAME)
    if old_sid:
        destroy_admin_session(request.app.state.rdb, old_sid)
    
    new_sid = create_admin_session(request.app.state.rdb, username, ip)
    set_session_cookie(response, new_sid)
    return {"status": "ok"}

@app.post("/admin/logout")
async def admin_logout(request: Request, response: Response,
                       session: dict = Depends(require_admin)):
    sid = request.cookies.get(ADMIN_COOKIE_NAME)
    if sid:
        destroy_admin_session(request.app.state.rdb, sid)
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/admin")
    return {"status": "ok"}
```

**❌ НЕ робити:**
```python
# JWT у localStorage — XSS може вкрасти токен
localStorage.setItem('token', response.token)  # НЕБЕЗПЕЧНО

# Зберігання пароля в .env plain text
ADMIN_PASSWORD=mypassword123  # НЕБЕЗПЕЧНО — треба bcrypt hash

# Як правильно згенерувати хеш для .env:
# python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpass', bcrypt.gensalt()).decode())"
# ADMIN_PASSWORD_HASH=$2b$12$...
```

---

### 1.2 CSRF захист

`samesite="strict"` допомагає, але не достатньо для AJAX-запитів. Потрібен Double Submit CSRF token.

```python
import hmac, hashlib

def generate_csrf_token(session_id: str, secret: str) -> str:
    return hmac.new(
        secret.encode(), session_id.encode(), hashlib.sha256
    ).hexdigest()

def verify_csrf_token(session_id: str, token: str, secret: str) -> bool:
    expected = generate_csrf_token(session_id, secret)
    return hmac.compare_digest(expected, token)

class CSRFMiddleware(BaseHTTPMiddleware):
    EXEMPT_PATHS = {"/admin/login", "/health"}
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method in self.SAFE_METHODS or path in self.EXEMPT_PATHS:
            return await call_next(request)
        if not path.startswith("/admin"):
            return await call_next(request)
        
        session_id = request.cookies.get(ADMIN_COOKIE_NAME, "")
        csrf_token = request.headers.get("X-CSRF-Token", "")
        
        if not csrf_token or not verify_csrf_token(
            session_id, csrf_token, os.getenv("CSRF_SECRET", "")
        ):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        
        return await call_next(request)

app.add_middleware(CSRFMiddleware)
```

```html
<!-- В HTML адмінки: CSRF токен у meta -->
<meta name="csrf-token" content="{{ csrf_token }}">
<script>
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
// Обгортка для fetch — завжди додає CSRF токен
async function adminFetch(url, options = {}) {
    return fetch(url, {
        ...options,
        headers: { ...options.headers, 'X-CSRF-Token': csrfToken },
        credentials: 'same-origin'
    });
}
</script>
```

---

### 1.3 Brute-force захист

Slowapi (rate limit по IP) — перший рівень. Потрібен другий рівень — по username.

```python
BRUTE_FORCE_THRESHOLD = 5   # спроб до блокування
BRUTE_FORCE_WINDOW = 300    # 5 хвилин вікно
BRUTE_FORCE_LOCKOUT = 900   # 15 хвилин блокування

def check_brute_force(rdb, identifier: str) -> bool:
    return bool(rdb.exists(f"bf_lock:{identifier}"))

def record_failed_attempt(rdb, ip: str, username: str):
    for ident in [f"ip:{ip}", f"user:{username}"]:
        pipe = rdb.pipeline()
        pipe.incr(f"bf_attempts:{ident}")
        pipe.expire(f"bf_attempts:{ident}", BRUTE_FORCE_WINDOW)
        count, _ = pipe.execute()
        if count >= BRUTE_FORCE_THRESHOLD:
            rdb.setex(f"bf_lock:{ident}", BRUTE_FORCE_LOCKOUT, "1")
            logger.warning("BRUTE_FORCE_LOCKOUT ident=%s count=%d", ident, count)

def clear_brute_force(rdb, ip: str, username: str):
    for ident in [f"ip:{ip}", f"user:{username}"]:
        rdb.delete(f"bf_attempts:{ident}", f"bf_lock:{ident}")

# В login endpoint:
@app.post("/admin/login")
@limiter.limit("10/minute")  # Перший рівень (slowapi)
async def admin_login(request: Request, response: Response,
                      username: str = Form(), password: str = Form()):
    ip = get_remote_address(request)
    rdb = request.app.state.rdb
    
    # Другий рівень
    if check_brute_force(rdb, f"ip:{ip}"):
        raise HTTPException(429, "Заблоковано. Спробуйте через 15 хв.")
    if check_brute_force(rdb, f"user:{username}"):
        raise HTTPException(429, "Акаунт тимчасово заблоковано. Спробуйте через 15 хв.")
    
    # ... перевірка пароля ...
    # Якщо невдача:
    record_failed_attempt(rdb, ip, username)
    await asyncio.sleep(0.5)  # Уповільнення перебору
    raise HTTPException(401, "Невірні дані")
    
    # Якщо успіх:
    clear_brute_force(rdb, ip, username)
    # ... створення сесії ...
```

---

## 2. receiver_key: ентропія та захист від перебору

### Аналіз ентропії

`secrets.token_urlsafe(N)` генерує криптографічно безпечні токени з `/dev/urandom`.

| `N` (байт) | ~Символів | Ентропія | Brute force @ 10K req/s |
|-----------|----------|----------|------------------------|
| 8 | ~11 | 64 біт | ~58,000 років |
| 12 | ~16 | 96 біт | ~2.5×10¹⁷ років |
| **16** | **~22** | **128 біт** | **~1.07×10²⁴ років** ← рекомендую |
| 32 | ~43 | 256 біт | практично нескінченно |

```python
def generate_receiver_key() -> str:
    """128 біт ентропії — достатньо, але не занадто довгий URL"""
    return secrets.token_urlsafe(16)

# ⚠️ Зберігати ТІЛЬКИ SHA-256 хеш в БД, не plain text
import hashlib

def hash_receiver_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

def verify_receiver_key(provided: str, stored_hash: str) -> bool:
    """Timing-safe порівняння"""
    return secrets.compare_digest(
        hash_receiver_key(provided), stored_hash
    )

# Rate limit на endpoint з receiver_key
@app.get("/pay/{receiver_key}")
@limiter.limit("20/minute")
async def pay_by_key(request: Request, receiver_key: str):
    if not re.match(r'^[A-Za-z0-9_-]{20,30}$', receiver_key):
        raise HTTPException(400, "Невірний формат ключа")
    
    receiver = await db.get_by_key_hash(hash_receiver_key(receiver_key))
    if not receiver:
        await asyncio.sleep(0.1)  # Однаковий час відповіді
        raise HTTPException(404, "Не знайдено")
    
    return render_pay_page(receiver)
```

**❌ НЕ робити:**
```python
receiver_key = str(user_id)           # Перебирається тривіально!
receiver_key = f"rcv_{user_id}"       # Теж небезпечно
db.save(key=receiver_key)             # Зберігати plain text в БД!
logger.info("key=%s", receiver_key)   # Витік у логи!

# Правильно логувати:
logger.info("key_prefix=%s...", receiver_key[:6])
```

---

## 3. Брендинг через адмінку: XSS захист

### Вектори атаки

1. `brand_name` = `<script>alert(1)</script>` → stored XSS на платіжній сторінці покупця
2. `brand_color` = CSS injection (`expression()`, `url(javascript:...)`)
3. `logo_url` = `javascript:alert(1)` або `data:text/html,...`

### Захист через Pydantic валідацію

```python
from pydantic import BaseModel, field_validator
import re, html

class BrandingConfig(BaseModel):
    brand_name: str
    brand_tagline: str
    brand_color: str
    logo_url: str | None = None

    @field_validator("brand_name", "brand_tagline")
    @classmethod
    def sanitize_text(cls, v):
        v = v.strip()
        if len(v) > 100:
            raise ValueError("Занадто довге")
        # Заборонені символи для HTML/JS контексту
        if re.search(r'[<>"\'\'`&]', v):
            raise ValueError("Заборонені символи")
        return v

    @field_validator("brand_color")
    @classmethod
    def validate_color(cls, v):
        # ТІЛЬКИ hex: #RGB або #RRGGBB
        if not re.match(r'^#[0-9A-Fa-f]{3}([0-9A-Fa-f]{3})?$', v):
            raise ValueError("Формат: #RRGGBB")
        return v

    @field_validator("logo_url")
    @classmethod
    def validate_logo_url(cls, v):
        if v is None:
            return v
        # Тільки HTTPS, без javascript: та data:
        if not re.match(r'^https://[a-zA-Z0-9][a-zA-Z0-9._/-]+\.[a-zA-Z]{2,}', v):
            raise ValueError("URL має починатись з https://")
        # Перевірка розширення
        path = v.split('?')[0].lower()
        if not any(path.endswith(e) for e in ('.png','.jpg','.jpeg','.webp','.svg')):
            raise ValueError("Тільки PNG/JPG/WEBP/SVG")
        return v

# Вставка в HTML — завжди через html.escape()
def render_branding(brand: BrandingConfig) -> str:
    safe_name = html.escape(brand.brand_name)
    safe_tagline = html.escape(brand.brand_tagline)
    safe_color = html.escape(brand.brand_color)  # вже hex, але для впевненості
    
    logo_html = ""
    if brand.logo_url:
        safe_url = html.escape(brand.logo_url)
        logo_html = f'<img src="{safe_url}" alt="" loading="lazy">'
    
    return f"""
    <style>:root {{ --brand: {safe_color}; }}</style>
    {logo_html}
    <h1>{safe_name}</h1>
    <p>{safe_tagline}</p>
    """
```

**CSP для логотипів:** якщо логотип зовнішній — обмежити img-src:
```
Content-Security-Policy: img-src 'self' data: https://cdn.yourdomain.com
```
НЕ дозволяти `img-src *` — це відкриває leakage через tracking pixels.

---

## 4. API Keys: безпечне зберігання та ротація

### Проблеми поточного підходу

- Один ключ `API_KEY` для всіх клієнтів
- Немає механізму ротації
- Якщо скомпрометовано — треба міняти скрізь одночасно

### Правильна архітектура (multi-key з ротацією)

```python
import hashlib, time

def create_api_key(rdb, label: str, created_by: str) -> str:
    """Створює ключ, повертає plain text ОДИН РАЗ"""
    raw_key = f"vp_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    
    rdb.hset(f"apikey:{key_hash}", mapping={
        "label": label,
        "created_by": created_by,
        "created_at": int(time.time()),
        "last_used": 0,
        "call_count": 0,
        "active": "1"
    })
    rdb.sadd("apikeys:all", key_hash)
    
    logger.info("API_KEY_CREATED label=%s by=%s", label, created_by)
    return raw_key  # Показати адміну ОДИН РАЗ — потім недоступний

def verify_api_key(rdb, provided_key: str) -> bool:
    key_hash = hashlib.sha256(provided_key.encode()).hexdigest()
    data = rdb.hgetall(f"apikey:{key_hash}")
    if not data or data.get("active") != "1":
        return False
    # Оновити статистику атомарно
    pipe = rdb.pipeline()
    pipe.hset(f"apikey:{key_hash}", "last_used", int(time.time()))
    pipe.hincrby(f"apikey:{key_hash}", "call_count", 1)
    pipe.execute()
    return True

def revoke_api_key(rdb, key_hash: str):
    rdb.hset(f"apikey:{key_hash}", "active", "0")
    rdb.hset(f"apikey:{key_hash}", "revoked_at", int(time.time()))
    logger.warning("API_KEY_REVOKED hash=%s...", key_hash[:8])
```

### Процедура ротації (zero-downtime)

```bash
# 1. Створити новий ключ через адмінку
POST /admin/apikeys/create
{"label": "n8n-prod-v2", "created_by": "admin"}
# Відповідь: {"key": "vp_..."}  ← зберегти, більше не показуємо!

# 2. Оновити ключ в n8n (паралельно зі старим)
# 3. Через 7 днів відкликати старий
DELETE /admin/apikeys/{old_key_hash}
```

**❌ НЕ робити:**
```python
API_KEY = "hardcoded_secret"         # В коді — потрапляє в git!
logger.info("key: %s", api_key)     # Витік у логи
GET /generate?api_key=secret         # В nginx access.log!
```

---

## 5. PostgreSQL: connection security та SQL injection

### Connection Security

```bash
# .env
DATABASE_URL=postgresql+asyncpg://vilnopay:${DB_PASSWORD}@postgres:5432/vilnopay?sslmode=require
```

```yaml
# docker-compose.yml
postgres:
  image: postgres:16-alpine
  environment:
    POSTGRES_DB: vilnopay
    POSTGRES_USER: vilnopay
    POSTGRES_PASSWORD: ${DB_PASSWORD}
  command: >
    postgres
    -c ssl=on
    -c log_connections=on
    -c log_min_duration_statement=1000
  networks:
    - internal  # ТІЛЬКИ internal, не proxy!
```

```sql
-- Мінімальні привілеї (принцип least privilege):
CREATE USER vilnopay WITH PASSWORD 'strong_password';
GRANT CONNECT ON DATABASE vilnopay TO vilnopay;
GRANT USAGE ON SCHEMA public TO vilnopay;
GRANT SELECT, INSERT, UPDATE ON TABLE receivers, api_keys TO vilnopay;
-- НЕ давати: DROP, TRUNCATE, CREATE, SUPERUSER
```

### SQL Injection

**Prisma (Node.js) — завжди параметризовано:**
```typescript
// ✅ БЕЗПЕЧНО:
const r = await prisma.receiver.findUnique({ where: { key_hash: hash } });
const r = await prisma.$queryRaw`SELECT * FROM receivers WHERE id = ${id}`;

// ❌ НЕБЕЗПЕЧНО:
const r = await prisma.$queryRawUnsafe(
    `SELECT * FROM receivers WHERE name = '${userInput}'`
);
```

**psycopg (Python) — завжди %s параметри:**
```python
# ✅ БЕЗПЕЧНО:
cursor.execute("SELECT * FROM receivers WHERE key_hash = %s", (key_hash,))
await conn.fetchrow("SELECT * FROM receivers WHERE key_hash = $1", key_hash)

# ❌ НЕБЕЗПЕЧНО:
cursor.execute(f"SELECT * FROM receivers WHERE key_hash = '{key_hash}'")
cursor.execute("SELECT * FROM receivers WHERE key_hash = '%s'" % key_hash)

# ⚠️ Динамічне ORDER BY — тільки whitelist:
ALLOWED_SORT = {"created_at", "name"}
order_col = request.query_params.get("sort", "created_at")
if order_col not in ALLOWED_SORT:
    order_col = "created_at"
# Не format() для ORDER BY!
```

---

## 6. Session Fixation та Session Hijacking

### Session Fixation

Атака: зловмисник встановлює відому session_id до логіну, потім після логіну — отримує доступ.

**Захист: ЗАВЖДИ регенерувати session_id після успішного логіну:**
```python
@app.post("/admin/login")
async def admin_login(request: Request, response: Response, ...):
    # ... перевірка пароля ...
    
    # КРИТИЧНО: знищити стару сесію
    old_sid = request.cookies.get(ADMIN_COOKIE_NAME)
    if old_sid:
        destroy_admin_session(rdb, old_sid)
    
    # Нова сесія — нова session_id
    new_sid = create_admin_session(rdb, username, ip)
    set_session_cookie(response, new_sid)
```

### Session Hijacking

Механізм: зловмисник краде cookie (XSS або мережа) і використовує чужу сесію.

**Захист 1 — IP binding (м'яке):**
```python
def create_admin_session(rdb, admin_id: str, ip: str) -> str:
    session_id = secrets.token_urlsafe(32)
    rdb.setex(f"admin_session:{session_id}", ADMIN_SESSION_TTL, json.dumps({
        "admin_id": admin_id,
        "ip": ip,
        "ua_hash": None
    }))
    return session_id

async def require_admin(request: Request) -> dict:
    session_id = request.cookies.get(ADMIN_COOKIE_NAME)
    if not session_id:
        raise HTTPException(401, "Необхідна авторизація")
    session = get_admin_session(request.app.state.rdb, session_id)
    if not session:
        raise HTTPException(401, "Сесія закінчилась")
    
    # М'яка перевірка IP: якщо змінився — логуємо, але не блокуємо
    # (IP може змінитись через мобільний інтернет)
    current_ip = get_remote_address(request)
    if session.get("ip") and session["ip"] != current_ip:
        logger.warning("SESSION_IP_CHANGE sid_prefix=%s old=%s new=%s",
                       session_id[:6], session["ip"], current_ip)
        # Для суворішого захисту: raise HTTPException(401, "IP змінився")
    
    return session
```

**Захист 2 — короткий TTL + sliding window:**
- TTL сесії: 1 година активності
- Абсолютний максимум: 8 годин
- Після 8 годин — примусовий logout

```python
def get_admin_session(rdb, session_id: str) -> dict | None:
    raw = rdb.get(f"admin_session:{session_id}")
    if not raw:
        return None
    data = json.loads(raw)
    
    # Абсолютний timeout: 8 годин
    if int(time.time()) - data["created_at"] > 8 * 3600:
        rdb.delete(f"admin_session:{session_id}")
        return None
    
    # Sliding window: продовжуємо TTL при активності
    rdb.expire(f"admin_session:{session_id}", ADMIN_SESSION_TTL)
    return data
```

**Захист 3 — HttpOnly + Secure cookie (вже є в рекомендаціях вище)**

---

## 7. File Upload для логотипу: валідація

### Вектори атак

1. SVG з вбудованим JS: `<svg><script>alert(1)</script></svg>`
2. Поліглот файл (PNG + PHP код)
3. DoS через гігантські файли
4. Path traversal: `filename = "../../app.py"`
5. SVG XXE: `<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>`

### Повна валідація завантаження

```python
import magic  # pip install python-magic
import hashlib
from PIL import Image  # pip install Pillow
import io

MAX_LOGO_SIZE = 2 * 1024 * 1024   # 2MB
MAX_DIMENSIONS = (2048, 2048)      # пікселі
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}  # SVG — заборонено!

@app.post("/admin/upload-logo")
@limiter.limit("5/minute")
async def upload_logo(request: Request, file: UploadFile,
                      session: dict = Depends(require_admin)):
    # 1. Перевірка розміру до читання
    content = await file.read(MAX_LOGO_SIZE + 1)
    if len(content) > MAX_LOGO_SIZE:
        raise HTTPException(400, f"Файл занадто великий (max {MAX_LOGO_SIZE // 1024 // 1024}MB)")
    if len(content) == 0:
        raise HTTPException(400, "Порожній файл")
    
    # 2. Перевірка MIME через magic bytes (не Content-Type з заголовка!)
    detected_mime = magic.from_buffer(content, mime=True)
    if detected_mime not in ALLOWED_MIME:
        raise HTTPException(400, f"Дозволені тільки PNG/JPG/WEBP. Виявлено: {detected_mime}")
    
    # 3. Валідація через PIL (захист від поліглотів)
    try:
        img = Image.open(io.BytesIO(content))
        img.verify()  # Перевіряє цілісність зображення
    except Exception:
        raise HTTPException(400, "Пошкоджене зображення")
    
    # 4. Перевірка розмірів пікселів
    img = Image.open(io.BytesIO(content))  # Reopen після verify()
    if img.width > MAX_DIMENSIONS[0] or img.height > MAX_DIMENSIONS[1]:
        raise HTTPException(400, f"Занадто велике зображення (max {MAX_DIMENSIONS[0]}x{MAX_DIMENSIONS[1]})")
    
    # 5. Безпечна назва файлу (ігноруємо оригінальну)
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    ext = ext_map[detected_mime]
    safe_filename = f"logo_{hashlib.sha256(content).hexdigest()[:16]}{ext}"
    
    # 6. Зберігати поза web root або сервити з окремого ендпоінту
    upload_dir = Path("/app/uploads/logos").resolve()
    dest = (upload_dir / safe_filename).resolve()
    
    # Захист від path traversal
    if not str(dest).startswith(str(upload_dir)):
        raise HTTPException(400, "Недопустимий шлях")
    
    dest.write_bytes(content)
    
    logger.info("LOGO_UPLOADED admin=%s file=%s size=%d",
                session["admin_id"], safe_filename, len(content))
    return {"url": f"/static/logos/{safe_filename}"}
```

**❌ НЕ робити:**
```python
# НЕ довіряти Content-Type з заголовка HTTP
if file.content_type == "image/png":  # ← підроблюється!

# НЕ зберігати SVG (XSS, XXE):
if file.content_type == "image/svg+xml": ...  # ЗАБОРОНЕНО!

# НЕ використовувати ім'я з завантаженого файлу:
save_path = f"/app/uploads/{file.filename}"  # ← path traversal!
```

---

## 8. Rate Limiting на адмін-ендпоінти

### Поточний стан

У `app.py` v3.0 налаштовано:
- `/generate` — 10/хв 
- `/qr.png` — 20/хв
- `/p/{id}` — 30/хв
- `/health` — 30/хв

Адмін-ендпоінти не існують ще. При додаванні — обов'язкова конфігурація:

```python
# Рекомендовані ліміти для адмін-ендпоінтів:

@app.post("/admin/login")
@limiter.limit("5/minute")      # Дуже суворо: brute-force
async def admin_login(...): ...

@app.get("/admin/dashboard")
@limiter.limit("60/minute")     # Звичайний browse
async def dashboard(...): ...

@app.post("/admin/apikeys/create")
@limiter.limit("10/minute")     # Середній: адмінська операція
async def create_api_key(...): ...

@app.post("/admin/upload-logo")
@limiter.limit("5/minute")      # Суворо: upload CPU-expensive
async def upload_logo(...): ...

@app.get("/admin/receivers")
@limiter.limit("30/minute")
async def list_receivers(...): ...
```

### Nginx rate limiting для адмінки

```nginx
# Окрема зона для адмін-панелі
limit_req_zone $binary_remote_addr zone=admin_api:10m rate=30r/m;
limit_req_zone $binary_remote_addr zone=admin_login:10m rate=5r/m;

server {
    # ...
    
    # Адмін-панель: суворіший ліміт
    location /admin/ {
        limit_req zone=admin_api burst=10 nodelay;
        
        # Додатково: дозволити тільки внутрішні IP
        # allow 10.0.0.0/8;  # VPN мережа
        # allow 192.168.1.0/24;  # офіс
        # deny all;
        
        proxy_pass http://127.0.0.1:8099;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    
    location /admin/login {
        limit_req zone=admin_login burst=3 nodelay;
        proxy_pass http://127.0.0.1:8099;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

**Найкращий захист адмінки: обмежити доступ по IP або через VPN.**

```nginx
location /admin/ {
    # Тільки з вашого VPN або офісу
    allow 10.8.0.0/24;    # WireGuard VPN
    allow 192.168.1.0/24; # офісна мережа
    deny all;             # всі інші — 403
    
    proxy_pass http://127.0.0.1:8099;
}
```

---

## Фінальний чек-лист для v4.0

### 🔴 КРИТИЧНО (перед деплоєм)

- [ ] Session cookie: `httponly=True, secure=True, samesite="strict", path="/admin"`
- [ ] Регенерація session_id після логіну (session fixation)
- [ ] bcrypt для паролів адміна (не plain text)
- [ ] CSRF токен для всіх POST/PUT/DELETE /admin/*
- [ ] Brute-force по IP та username (Redis)
- [ ] receiver_key: `secrets.token_urlsafe(16)` + зберігати SHA-256 хеш
- [ ] XSS: html.escape() для ВСІХ user-input полів у шаблонах
- [ ] File upload: magic bytes + PIL.verify() + заборонити SVG
- [ ] PostgreSQL: sslmode=require + least privilege user
- [ ] Адмінка доступна тільки з VPN або по IP whitelist

### 🟠 ВАЖЛИВО (протягом тижня)

- [ ] Multi-key API keys з ротацією через адмінку
- [ ] Rate limiting на всі /admin/* ендпоінти
- [ ] Логування всіх адмін-дій (audit log)
- [ ] Абсолютний timeout для сесій (8 годин)
- [ ] Whitelist дозволених доменів для logo_url (або upload-only)

### 🟡 РЕКОМЕНДОВАНО (наступна ітерація)

- [ ] MFA для адміна (TOTP через pyotp)
- [ ] IP binding для сесій
- [ ] Сповіщення при suspicious login
- [ ] Автоматична ротація receiver_key кожні 90 днів
- [ ] PostgreSQL connection pooling (pgbouncer)

---

## Короткий підсумок знахідок у v3.0

| # | Файл | Вразливість | Критичність | Статус |
|---|------|-------------|-------------|--------|
| 1 | app.py | CSP дозволяє `unsafe-inline` для script-src | 🟠 Середній | Потребує фіксу |
| 2 | app.py | `default_limits=["100/minute"]` занадто ліберальний | 🟡 Низький | OK для MVP |
| 3 | docker-compose.yml | Redis expose на `internal` (не `proxy`) | ✅ Виправлено | |
| 4 | nginx | Відсутній `limit_req` для /admin (не існує ще) | N/A | Реалізувати |
| 5 | .env.example | Приклад містить placeholder паролі | 🟡 Інфо | ОК |

### CSP: виправити `unsafe-inline` для script-src

```python
# Поточно (небезпечно):
"script-src 'self' 'unsafe-inline'"

# Краще — nonce-based CSP:
import secrets

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        csp = (
            "default-src 'self'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'none';"
        )
        response.headers["Content-Security-Policy"] = csp
        return response
```

```html
<!-- В шаблоні: додати nonce до кожного script тегу -->
<script nonce="{{ request.state.csp_nonce }}">
    // ваш JS
</script>
```

---

*Аудит проведено автоматично на базі вихідного коду. Рекомендується ручна перевірка перед продакшеном.*