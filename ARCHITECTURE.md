# VilnoPayService v4.0 — Архітектура

## Зміни відносно v3.0

| Аспект | v3.0 | v4.0 |
|--------|------|------|
| Отримувач | PII в кожному запиті (receiver, iban, code) | `receiver_key` → дані з БД |
| Сховище | Тільки Redis | Redis (TTL-посилання) + PostgreSQL (config, receivers, keys) |
| Адмінка | Немає | Повна: /admin з логіном |
| API ключі | Один в env | Кілька, з хешуванням, ротацією |
| Брендинг | Захардкоджений | Налаштовуваний через адмінку |
| Аудит | Тільки логи | PostgreSQL payment_links_log |

## Структура файлів

```
VilnoPayService/
├── app.py              # FastAPI endpoints
├── db.py               # PostgreSQL helpers, CRUD
├── templates.py        # HTML-шаблони сторінки оплати
├── schema.sql          # DDL для PostgreSQL
├── admin.html          # SPA адмін-панелі
├── static/             # Статичні файли (логотипи)
├── docker-compose.yml  # Redis + PostgreSQL + App
├── Dockerfile
├── requirements.txt
├── .env.example
└── nginx/
    └── vilnopay.conf   # Reverse proxy конфігурація
```

## Схема БД (PostgreSQL)

### admin_users
Адміністратори системи. Пароль зберігається як bcrypt-хеш.

### admin_sessions
HTTP-only cookie-сесії. TTL = SESSION_TTL_HOURS (за замовч. 8 год).
Токен — `secrets.token_urlsafe(48)`.

### settings
Key-value сховище брендингу:
- `logo_url`, `bg_color`, `primary_color`, `accent_color`
- `page_title`, `page_subtitle`, `footer_text`
- `link_ttl_hours`, `custom_css`

### receivers
Отримувачі платежів. Кожен має унікальний `receiver_key` (формат `rcv_xxxxx`).
Містить: name, receiver (ПІБ), iban, edrpou, is_active.

### api_keys
API-ключі для /generate. Зберігається SHA-256 хеш ключа + перші 12 символів
для ідентифікації. Підтримка кількох ключів, ротація, відкликання.

### payment_links_log
Аудит-лог усіх згенерованих посилань: link_id, receiver_key, purpose,
amount, api_key_prefix, IP, час.

## API Endpoints

### Публічні
| Method | Path | Опис |
|--------|------|------|
| POST | `/generate` | Створити посилання (receiver_key + purpose + amount) |
| POST | `/upload-invoice` | Завантажити PDF-рахунок |
| POST | `/liqpay/callback` | Callback від LiqPay |
| GET | `/p/{link_id}` | Сторінка оплати |
| GET | `/invoice/{link_id}` | Завантажити прикріплений PDF |
| GET | `/liqpay/checkout-data/{link_id}` | Дані checkout для LiqPay |
| GET | `/liqpay/result/{link_id}` | Сторінка результату LiqPay |
| GET | `/health` | Статус сервісу |

### Адмін / менеджер (cookie-сесія)
| Method | Path | Опис |
|--------|------|------|
| GET | `/admin` | HTML-сторінка адмін-панелі |
| POST | `/admin/login` | Логін |
| POST | `/admin/logout` | Логаут |
| GET | `/admin/me` | Поточний користувач сесії |
| GET/PUT | `/admin/settings` | Брендинг |
| GET/POST | `/admin/receivers` | Список / створення отримувачів |
| PUT/DELETE | `/admin/receivers/{key}` | Оновлення / видалення |
| GET | `/admin/api-keys` | Список API-ключів |
| DELETE | `/admin/api-keys/{id}` | Відкликання ключа |
| GET | `/admin/links-log` | Аудит-лог |
| GET | `/admin/views-log` | Перегляди клієнтами |
| GET | `/admin/liqpay-transactions` | Лог транзакцій LiqPay |
| GET | `/manager` | HTML-кабінет менеджера |
| GET/POST | `/manager/templates` | Шаблони менеджера |
| DELETE | `/manager/templates/{id}` | Видалення шаблону |
| POST | `/manager/create-payment` | Створення платежу з кабінету |
| GET | `/manager/history` | Історія платежів менеджера |
| GET | `/manager/receivers` | Список отримувачів |

## Безпека

### Автентифікація
- Адмін: bcrypt-хеш пароля, HTTP-only + SameSite=Strict + Secure cookie
- API: SHA-256 хеш ключа, порівняння через `secrets.compare_digest`-подібний підхід
- Brute-force: rate limit 5/хв на /admin/login

### Захист від XSS
- Всі дані проходять через `html.escape()` перед вставкою в HTML
- CSP заголовки: `default-src 'self'`

### CSRF
- Cookie SameSite=Strict — браузер не надсилає cookie при cross-origin запитах
- Для ще кращого захисту: можна додати X-CSRF-Token header

### Session Management
- Токен: 48 байт urlsafe (`secrets.token_urlsafe(48)`)
- TTL: налаштовуваний (за замовч. 8 год)
- Старі сесії користувача видаляються при новому логіні
- Прив'язка до IP та User-Agent (лог)

### Дані отримувачів
- **Ключова зміна**: IBAN, ПІБ, ЄДРПОУ більше НЕ передаються через API
- Клієнт API надсилає лише `receiver_key` → сервер підставляє дані з БД
- Неможливо підмінити банківські реквізити через API-запит

### Інфраструктура
- PostgreSQL: окремий контейнер, internal network, healthcheck
- Redis: пароль, перейменовані небезпечні команди
- Docker: read-only FS, no-new-privileges, memory limits
- nginx: rate limiting, HSTS, TLS 1.2+

## Міграція з v3.0

1. Додати PostgreSQL до docker-compose
2. Задати PG_PASSWORD, DATABASE_URL, ADMIN_INIT_PASS в .env
3. `docker compose up -d` — schema.sql застосується автоматично
4. Зайти в /admin, додати отримувачів
5. Створити API-ключі замість єдиного API_KEY
6. Оновити клієнтів: замість `{receiver, iban, code, purpose, amount}`
   надсилати `{receiver_key, purpose, amount}`
7. Видалити ADMIN_INIT_PASS з .env після першого входу

## Нотатки по поточній реалізації

- LiqPay налаштовується **на рівні отримувача** (`receivers`), а не через окрему універсальну адмінку провайдерів.
- У `schema.sql` ще можуть лишатися таблиці/сліди раннього generic provider layer для сумісності з попередніми ітераціями, але активний runtime flow працює через receiver-level LiqPay config.
- Inline JS у `admin.html`, `manager.html` і частково у `templates.py` ще не винесений у static-файли; це окремий майбутній cleanup/security етап.

## Приклад запиту /generate (v4.0)

```bash
curl -X POST https://pay.yourdomain.com/generate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: vpk_xxxxxxxxxxxxx" \
  -d '{"receiver_key": "rcv_abc123", "purpose": "Оплата за товар #1234", "amount": "1500.00"}'
```

Відповідь:
```json
{
  "pay_url": "https://pay.yourdomain.com/p/aBcDeFgHiJkL",
  "nbu_url": "https://bank.gov.ua/qr/...",
  "qr_base64": "iVBORw0KGgo...",
  "expires_in_hours": 24
}
```
