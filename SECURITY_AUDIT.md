# Security Audit — VilnoPayService v4.0.0

**Дата:** 2026-06-29  
**Аудитор:** Automated Security Audit (Claude Opus)  
**Скоуп:** app.py, db.py, templates.py, schema.sql, admin.html, manager.html, docker-compose.yml, Dockerfile, .env.example

## Зведення

| Severity | К-сть |
|----------|-------|
| Critical | 5 |
| High | 8 |
| Medium | 10 |
| Low | 7 |
| **Всього** | **30** |

---

## CRITICAL

### C-1. liqpay_verify_callback — підпис ніколи не перевіряється

**Файл:** app.py:381

`liqpay_verify_callback(private_key, data, signature)` викликає `liqpay_signature(private_key, data, signature)` — 3 аргументи у функцію що приймає 2. Завжди TypeError. Callback `/liqpay/callback` впаде з 500. Підпис ніколи не верифікується.

```python
# ЗЛАМАНИЙ:
def liqpay_verify_callback(private_key, data, signature):
    return liqpay_signature(private_key, data, signature) == signature

# ФІКС:
import hmac
def liqpay_verify_callback(private_key, data, signature):
    expected = liqpay_signature(private_key, data)
    return hmac.compare_digest(expected, signature)
```

### C-2. LiqPay callback без IP whitelist

**Файл:** app.py:396

`/liqpay/callback` приймає POST від будь-якої IP. Разом з C-1 атакуючий може фальсифікувати callback і змінити статус транзакції на `success`.

```python
LIQPAY_ALLOWED_IPS = os.getenv("LIQPAY_CALLBACK_IPS",
    "52.57.206.155,18.196.206.69,18.157.73.187,3.126.23.189").split(",")

@app.post("/liqpay/callback")
async def liqpay_callback(request: Request):
    if get_remote_address(request) not in LIQPAY_ALLOWED_IPS:
        raise HTTPException(403, "Forbidden")
```

### C-3. Реальний Fernet-ключ у .env

**Файл:** .env

`.env` містить `PROVIDER_ENCRYPTION_KEY=5dRN-...`. Якщо потрапив у git-історію — всі LiqPay private keys можуть бути розшифровані.

```bash
git log --all --diff-filter=A -- .env
# Якщо знайдено — перегенерувати ключ і перешифрувати всі keys
```

### C-4. CSS injection з виходом з style-тега (Stored XSS)

**Файл:** app.py:188-193, templates.py:9

`custom_css` вставляється у `<style>` без escape. Payload: `</style><script>alert(document.cookie)</script>` — XSS на ВСІХ платіжних сторінках. Існуюча фільтрація обходиться через CSS escapes.

```python
# app.py — додати перевірки:
if re.search(r'</\s*style', css, re.IGNORECASE):
    raise HTTPException(400, "Заборонено")
if re.search(r'u\s*r\s*l\s*\(', css, re.IGNORECASE):
    raise HTTPException(400, "url() заборонено")
if re.search(r'@\w', css):
    raise HTTPException(400, "@-правила заборонені")

# templates.py — _css_vars():
cc = s.get("custom_css", "").replace("</", "<\\/")
```

### C-5. block_order валідація після збереження

**Файл:** app.py:194-202

`update_settings(filtered)` викликається ДО валідації `block_order`. Невалідні дані записуються в БД.

```python
# ФІКС — валідація перед update_settings():
if "block_order" in filtered:
    order = json.loads(filtered["block_order"])
    valid = {"nbu_qr", "liqpay", "requisites"}
    if not isinstance(order, list) or not all(b in valid for b in order):
        raise HTTPException(400, "Невірний block_order")
update_settings(filtered)
```

---

## HIGH

### H-1. Відсутність CSRF-захисту

**Файл:** app.py — всі POST/PUT/DELETE адмін/менеджер ендпоінти

Cookie auth без CSRF-токенів. SameSite=Strict частково захищає, але недостатньо для всіх браузерів/сценаріїв.

```python
# Double-submit cookie:
class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method in ("POST", "PUT", "DELETE"):
            if request.url.path.startswith(("/admin/", "/manager/")):
                if request.url.path not in ("/admin/login",):
                    cookie = request.cookies.get("csrf_token", "")
                    header = request.headers.get("X-CSRF-Token", "")
                    if not cookie or not hmac.compare_digest(cookie, header):
                        return JSONResponse({"detail": "CSRF"}, 403)
        return await call_next(request)
```

### H-2. Path-параметри без валідації

**Файл:** app.py:218,225,237

`receiver_key`, `key_id`, `manager_id` у path не валідуються. Хоча SQL injection неможливий (параметризовані запити), формат не перевіряється.

```python
def _validate_receiver_key(key):
    if not re.match(r"^rcv_[A-Za-z0-9_-]{4,40}$", key):
        raise HTTPException(400, "Невірний receiver_key")
    return key
```

### H-3. Відсутність account lockout

**Файл:** app.py:155

Rate limit 5/min за IP. Через ротацію IP — 300+ спроб/год. Немає lockout по username.

```python
def _check_login_lockout(username):
    if int(rdb.get(f"login_fail:{username}") or 0) >= 10:
        raise HTTPException(429, "Акаунт заблоковано")

def _record_login_failure(username):
    rdb.incr(f"login_fail:{username}")
    rdb.expire(f"login_fail:{username}", 900)
```

### H-4. receiver_key у шаблонах не валідується

**Файл:** app.py:306 (manager_create_template)

Менеджер може вказати довільний receiver_key при створенні шаблону.

```python
rcv = get_receiver_by_key(receiver_key)
if not rcv or not rcv["is_active"]:
    raise HTTPException(400, "Отримувач не знайдений")
```

### H-5. Session fixation

**Файл:** app.py:155

При логіні старі сесії не інвалідуються. Скомпрометований токен залишається валідним.

```python
pg_execute("DELETE FROM admin_sessions WHERE user_id = %s", (user["id"],))
token = create_admin_session(user["id"], ...)
```

### H-6. REDIS_URL без пароля

**Файл:** .env.example

`REDIS_URL=redis://redis:6379/0` — без пароля, хоча Redis вимагає `--requirepass`.

```
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
```

### H-7. Відсутність HSTS

**Файл:** app.py:67

Немає Strict-Transport-Security. Cookie `secure=True`, але без HSTS перший запит може бути перехоплений.

```python
resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
```

### H-8. Timing attack при логіні (user enumeration)

**Файл:** app.py:162

Якщо user не знайдений — bcrypt.checkpw не викликається. Різний час відповіді розкриває існуючі username.

```python
DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt()).decode()

user = pg_query(...)
pw_hash = user["password_hash"] if user else DUMMY_HASH
valid = bcrypt.checkpw(body.password.encode(), pw_hash.encode())
if not user or not valid:
    raise HTTPException(401, "Невірний логін або пароль")
```

---

## MEDIUM

### M-1. XSS через inline onclick в admin.html

**Файл:** admin.html:378

Template literals у `onclick` атрибутах. Потенційний вихід з контексту через спец-символи.

```javascript
// Замість onclick — data-атрибути + addEventListener
```

### M-2. Менеджер бачить всіх отримувачів

**Файл:** app.py:354

`/manager/receivers` повертає всіх отримувачів. Немає фільтрації по дозволеним для менеджера.

### M-3. PDF upload — мінімальна валідація

**Файл:** app.py:474

Лише перевірка `%PDF-`. PDF може містити JS, polyglot payload.

```python
if b"%%EOF" not in contents[-1024:]:
    raise HTTPException(400, "Невалідний PDF")
```

### M-4. Sensitive data в Redis незашифровані

**Файл:** app.py

`pay:{link_id}` зберігає IBAN, ІПН, ПІБ у відкритому вигляді в Redis.

### M-5. CSP unsafe-inline для script-src

**Файл:** app.py:72-81

`script-src 'unsafe-inline'` знижує ефективність CSP. Використати nonce-based CSP.

```python
nonce = secrets.token_urlsafe(16)
csp = f"script-src 'nonce-{nonce}'; ..."
```

### M-6. autocommit=True — відсутність атомарності

**Файл:** db.py:25

Кожен SQL — окрема транзакція. Створення менеджера + API ключа не атомарне — можливий inconsistency.

### M-7. upload_invoice — 5MB без сканування

**Файл:** app.py:464

5MB PDF без антивірусного сканування.

### M-8. Адмін ендпоінти з dict замість Pydantic

**Файл:** app.py:281,306

`/admin/managers`, `/manager/create-payment` приймають `body: dict` — довільний JSON без валідації.

```python
class ManagerCreate(BaseModel):
    username: str
    password: str
    name: str = ""
    @field_validator("username")
    @classmethod
    def val(cls, v):
        if not re.match(r"^[a-zA-Z0-9_]{3,30}$", v):
            raise ValueError("Невірний формат")
        return v
```

### M-9. Expired sessions не чистяться автоматично

**Файл:** db.py:187

`cleanup_expired_sessions()` існує але ніде не викликається.

```python
# Додати в lifespan:
cleanup_expired_sessions()
```

### M-10. Dockerfile — apt без pinning

**Файл:** Dockerfile:7

`fonts-dejavu` без версії — non-reproducible build.

---

## LOW

### L-1. --forwarded-allow-ips "*"

**Файл:** Dockerfile:14

Довіряє X-Forwarded-For від будь-якого IP. Обмежити до reverse proxy subnet.

```dockerfile
--forwarded-allow-ips "172.16.0.0/12,10.0.0.0/8"
```

### L-2. Redis healthcheck з паролем у process list

**Файл:** docker-compose.yml:38

```yaml
test: ["CMD-SHELL", "redis-cli --no-auth-warning -a $REDIS_PASSWORD ping"]
```

### L-3. Відсутній cap_drop у docker-compose

**Файл:** docker-compose.yml

```yaml
cap_drop:
  - ALL
```

### L-4. admin.html accept включає SVG

**Файл:** admin.html:68

`accept="...,image/svg+xml"` — SVG в client-side accept, хоча server блокує. Прибрати.

### L-5. API key plain text у HTTP response

**Файл:** db.py:210

`create_manager()` повертає API key у plain text через HTTP + JS alert().

### L-6. ADMIN_INIT_PASS "changeme"

**Файл:** .env.example

Дефолтний пароль `changeme`. Багато хто забуде змінити.

### L-7. Відсутній X-Robots-Tag для адмін-сторінок

**Файл:** app.py:67

Адмін-сторінки можуть індексуватись пошуковиками.

```python
if request.url.path.startswith(("/admin", "/manager")):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
```

---

## ПОЗИТИВНІ АСПЕКТИ (що зроблено правильно)

1. **SQL injection** — всі запити параметризовані через psycopg2 `%s` placeholders
2. **bcrypt** для хешування паролів
3. **Fernet encryption** для LiqPay private keys
4. **Magic bytes** перевірка для logo upload
5. **Rate limiting** через slowapi
6. **Docker read_only** filesystem
7. **no-new-privileges** security opt
8. **Resource limits** для контейнерів
9. **Internal network** для Redis/PostgreSQL
10. **Redis dangerous commands** перейменовані (FLUSHALL, DEBUG, CONFIG)
11. **HTML escape** через `html.escape()` у templates.py
12. **Security headers** (X-Content-Type-Options, X-Frame-Options, Referrer-Policy, CSP)
13. **SameSite=Strict + Secure + HttpOnly** cookies
14. **Input validation** через Pydantic для основних моделей
15. **Non-root user** у Dockerfile

---

## ПРІОРИТЕТИ ВИПРАВЛЕННЯ

### Негайно (блокери):
1. **C-1** — Виправити liqpay_verify_callback (баг у кількості аргументів)
2. **C-2** — Додати IP whitelist для LiqPay callback
3. **C-4** — Заборонити `</style>` у custom_css
4. **C-5** — Перенести валідацію block_order перед збереженням
5. **C-3** — Перевірити git-історію на .env, перегенерувати ключ

### Цього тижня:
6. **H-8** — Виправити timing attack (dummy hash)
7. **H-5** — Інвалідувати старі сесії при логіні
8. **H-3** — Додати account lockout
9. **H-7** — Додати HSTS заголовок
10. **H-6** — Виправити REDIS_URL в .env.example

### Найближчий спринт:
11. **H-1** — CSRF middleware
12. **H-2** — Валідація path-параметрів
13. **M-5** — Nonce-based CSP
14. **M-8** — Pydantic замість dict
15. **M-9** — Автоочистка expired sessions
