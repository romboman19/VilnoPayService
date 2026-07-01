# VilnoPayService

**Self-hosted платіжний сервіс: QR-коди НБУ + LiqPay + рахунки-фактури**

VilnoPayService — self-hosted веб-сервіс для створення платіжних посилань з QR-кодами (Постанова НБУ №97), інтеграції LiqPay для онлайн-оплати карткою/Google Pay/Apple Pay, та прикріплення рахунків-фактур (PDF).

## Ролі

| Роль | Доступ | Опис |
|------|--------|------|
| **Адмін** | `/admin` | Керує менеджерами, отримувачами, LiqPay-налаштуванням отримувачів, брендингом, переглядає логи |
| **Менеджер** | `/manager` | Створює платіжні посилання, шаблони, завантажує рахунки-фактури |
| **Клієнт** | `/p/{link_id}` | Оплачує через банк-додаток (QR), LiqPay (картка/GPay/APay), або переказом за реквізитами |

## Можливості

### Клієнт (сторінка оплати)
- 🏦 **QR НБУ** — оплата через мобільний додаток банку (Web Share API на Android)
- 💳 **LiqPay** — онлайн-оплата: віджет (inline) або кнопка (redirect)
  - Картка Visa/Mastercard, Google Pay, Apple Pay, Privat24
  - Методи налаштовуються в кабінеті LiqPay
- 📋 **Реквізити** — компактні картки з копіюванням в один клік
- 📄 **Рахунок-фактура** — завантаження PDF або посилання на зовнішній документ
- ⏱️ **Лічильник** — реальний час до закінчення посилання
- 📱 **Адаптивний дизайн** — mobile-first, iOS safe-area, clamp для великих сум

### Менеджер (веб-кабінет)
- 🔐 **Логін/пароль** — окрема роль, не має доступу до адмінки
- 💳 **Створення платежів** — обирає отримувача, призначення, суму → отримує посилання + QR
- 📄 **Рахунки-фактури** — завантаження PDF (до 5MB) або посилання на зовнішній документ
- 📋 **Шаблони** — пресети (отримувач + призначення + сума за замовч.)
- 📊 **Історія** — список створених платежів з кнопкою копіювання посилання
- 🔑 **API ключ** — генерується автоматично, show-once (для n8n, інтеграцій)

### Адмін
- 👥 **Менеджери** — створення/видалення/вмикання, авто-генерація API ключів
- 🏢 **Отримувачі** — CRUD (ПІБ, IBAN, ЄДРПОУ/ІПН) + налаштування LiqPay per-receiver
- 💳 **LiqPay per-receiver** — налаштовується окремо для кожного отримувача:
  - Public Key, Private Key (зашифрований Fernet)
  - Режим: віджет (inline) або кнопка (redirect)
  - Sandbox режим
- 🎨 **Брендинг** — логотип, кольори, шрифт, CSS, порядок блоків на сторінці оплати
- 🔑 **API ключі** — перегляд + відкликання
- 📊 **Логи** — генерація посилань, перегляди клієнтами (IP, пристрій, банк)
- 👁️ **Preview** — попередній перегляд сторінки оплати

### Безпека
- **API ключі** — SHA-256 hash в БД, show-once модель
- **LiqPay** — private_key шифрований Fernet (AES-128 + HMAC), callback з верифікацією підпису (SHA-1 + hmac.compare_digest), IP whitelist
- **Сесії** — bcrypt, httponly cookies, secure, samesite=strict, session fixation захист
- **Account lockout** — 10 невдалих спроб → блок на 15 хвилин
- **CSP** — Content-Security-Policy (LiqPay домени в whitelist)
- **CSS injection** — escape `</` в custom_css
- **Rate limiting** — slowapi (10/хв generate, 30/хв pay page)
- **Docker** — read-only container, no-new-privileges, tmpfs /tmp
- **Role-based access** — адмін ≠ менеджер (автоматичний редирект)
- **PostgreSQL** — параметризовані запити (SQL injection захищений)

## Швидкий старт

### 1. Підготовка

```bash
git clone https://github.com/romboman19/VilnoPayService.git
cd VilnoPayService
cp .env.example .env
# Відредагувати .env:
# - ADMIN_INIT_USER / ADMIN_INIT_PASS — логін/пароль адміна
# - BASE_URL — публічна URL (напр. https://pay.example.com)
# - PG_PASSWORD, REDIS_PASSWORD
# - PROVIDER_ENCRYPTION_KEY — Fernet ключ для шифрування LiqPay private keys
#   Згенерувати: docker exec pay-service python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 2. Запуск

```bash
docker compose up -d --build
```

### 3. Налаштування (через адмінку, без коду)

1. `/admin` → залогіньтесь як адмін
2. **Отримувачі** → додайте отримувача (ПІБ, IBAN, ЄДРПОУ/ІПН)
   - Оберіть провайдер: **LiqPay** → введіть Public Key, Private Key, оберіть режим
3. **Менеджери** → створіть менеджера → **збережіть API ключ** (показується один раз!)
4. **Брендинг** → завантажте логотип, налаштуйте кольори, порядок блоків
5. Менеджер: `/manager` → створює платіж → копіює посилання → надсилає клієнту

## API

### POST /generate

Створює платіжне посилання. Вимагає менеджерський API ключ (`vpk_...`).

```bash
curl -X POST https://pay.domain.com/generate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: vpk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  -d '{
    "receiver_key": "rcv_abc123",
    "purpose": "За товар HUNTER",
    "amount": "1500",
    "invoice_id": "abc123",
    "invoice_url": "https://example.com/invoice.pdf"
  }'
```

**Відповідь:**

```json
{
  "pay_url": "https://pay.domain.com/p/abc123XYZ...",
  "nbu_url": "https://bank.gov.ua/qr/QkNE...",
  "qr_base64": "iVBOR...",
  "expires_in_hours": 24,
  "invoice_url": "https://pay.domain.com/invoice/abc123XYZ..."
}
```

**Поля запиту:**

| Параметр | Тип | Опис |
|----------|-----|------|
| `receiver_key` | string | Ключ отримувача (з адмінки) |
| `purpose` | string | Призначення платежу (2-420 символів) |
| `amount` | string? | Сума (опціонально) |
| `invoice_id` | string? | ID завантаженого PDF рахунку |
| `invoice_url` | string? | Зовнішнє посилання на рахунок |

### POST /upload-invoice

Завантаження PDF рахунку-фактури (до 5MB, magic bytes перевірка). Приймає API ключ або менеджерську cookie сесію.

### POST /liqpay/callback

Server-to-server callback від LiqPay. Верифікація підпису + IP whitelist.

### GET /p/{link_id}

Платіжна сторінка: QR НБУ + LiqPay (якщо налаштований) + реквізити + рахунок-фактура.

### GET /health

Статус сервісу (Redis + PostgreSQL).

## Архітектура

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Nginx     │────▶│   FastAPI    │────▶│   Redis     │
│  (proxy)    │     │  (app.py)    │     │  (TTL links)│
└─────────────┘     └──────┬───────┘     └─────────────┘
                           │
                    ┌──────┴───────┐
                    │  PostgreSQL  │
                    │  (managers,  │
                    │   receivers, │
                    │   LiqPay tx, │
                    │   keys,      │
                    │   templates, │
                    │   tx, logs)  │
                    └──────────────┘
```

| Компонент | Призначення |
|-----------|-------------|
| **FastAPI** | REST API, сторінка оплати, адмінка, кабінет менеджера |
| **Redis** | Зберігання платіжних посилань з TTL, AOF persistence |
| **PostgreSQL** | Менеджери, отримувачі, API-ключі, шаблони, транзакції LiqPay, логи, брендинг |
| **Docker** | read-only container, tmpfs /tmp, volumes /data/static + /data/invoices |

## Файли

| Файл | Опис |
|------|------|
| `app.py` | FastAPI: endpoint'и, middleware, LiqPay signature, callback |
| `db.py` | PostgreSQL: CRUD, міграції, Fernet шифрування, connection pool |
| `templates.py` | HTML-шаблони: сторінка оплати, LiqPay блок, результат, expired |
| `admin.html` | SPA адмін-панель (отримувачі, менеджери, брендинг, блоки) |
| `manager.html` | SPA кабінет менеджера (платежі, історія, шаблони) |
| `schema.sql` | DDL: таблиці, індекси, тригери |
| `docker-compose.yml` | Redis + PostgreSQL + App |
| `Dockerfile` | Python 3.12-slim + fonts-dejavu (для ₴ на QR) |

## Налаштування (.env)

| Змінна | Опис | За замовчуванням |
|--------|------|------------------|
| `BASE_URL` | Публічна URL сервісу | `http://localhost:8000` |
| `REDIS_URL` | URL Redis | `redis://redis:6379/0` |
| `DATABASE_URL` | URL PostgreSQL | `postgresql://vilnopay:vilnopay@postgres:5432/vilnopay` |
| `ADMIN_INIT_USER` | Логін адміна (при першому старті) | `admin` |
| `ADMIN_INIT_PASS` | Пароль адміна (при першому старті) | — |
| `SESSION_TTL_HOURS` | Час життя сесії (год) | `8` |
| `ALLOWED_HOSTS` | Дозволені хости | `*` |
| `LOG_LEVEL` | Рівень логування | `INFO` |
| `PROVIDER_ENCRYPTION_KEY` | Fernet ключ для шифрування LiqPay private keys | — |
| `LIQPAY_CALLBACK_IPS` | IP whitelist для LiqPay callback (через кому) | — |

## Відповідність НБУ №97

| Вимога | Статус |
|--------|--------|
| EPC QR Code v002 | ✅ |
| Win1251 кодування | ✅ |
| BCD / 002 / UCT | ✅ |
| IBAN 29 символів | ✅ |
| UAH + сума | ✅ |
| ЄДРПОУ/ІПН | ✅ |
| Base64URL + bank.gov.ua/qr/ | ✅ |
| Знак ₴ на QR-коді | ✅ |
| Отримувач max 140 символів | ✅ |
| Призначення max 420 символів | ✅ |
| Max 507 байт | ✅ |

## Ліцензія

AGPL-3.0 — див. [LICENSE](LICENSE) або https://www.gnu.org/licenses/agpl-3.0.html