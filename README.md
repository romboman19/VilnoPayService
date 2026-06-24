# VilnoPayService

**Сервіс генерації платіжних QR-кодів за стандартом НБУ №97 (EPC QR Code v002)**

VilnoPayService — це self-hosted веб-сервіс для створення платіжних посилань з QR-кодами, які відповідають Постанові НБУ №97 від 19.08.2025. Дозволяє продавцям генерувати QR-коди для кредитових переказів, які клієнти оплачують через мобільні додатки банків.

## Можливості

- 📤 **Генерація QR-кодів** за стандартом НБУ v002 (EPC QR Code, Base64URL, Win1251)
- 🏦 **Оплата через додаток банку** — Web Share API для передачі QR-зображення в банк-додаток
- 📷 **Сканування QR-кодом** — камерою або натисканням на зображення
- 📋 **Реквізити для переказу** — копіювання в один клік (Отримувач, IBAN, ІПН, Призначення, Сума)
- ⏱️ **Лічильник зворотного часу** — реальний час до закінчення посилання
- 🎨 **Брендинг** — логотип, кольори (фон, текст, блоки, межі), шрифт (Inter), розмір
- 🔐 **Адмін-панель** — отримувачі, API-ключі, брендинг, логи
- 📊 **Аналітика** — лог генерації посилань + лог переглядів клієнтами (IP, пристрій, банк)
- 🔑 **API-ключі** — SHA-256 хешування, ротація, кілька ключів з мітками
- 🛡️ **Безпека** — bcrypt сесії, CSP, rate limiting, read-only container, no-new-privileges

## Швидкий старт

### 1. Підготовка

```bash
git clone https://github.com/romboman19/VilnoPayService.git
cd VilnoPayService
cp .env.example .env
# Відредагувати .env:
# - ADMIN_INIT_USER / ADMIN_INIT_PASS — логін/пароль адміна (тільки при першому старті)
# - BASE_URL — публічна URL (напр. https://pay.example.com)
# - PG_PASSWORD, REDIS_PASSWORD
# - ALLOWED_HOSTS — домени через кому (або *)
```

### 2. Запуск

```bash
docker compose up -d --build
```

Сервіс доступний на порті `8099` (або налаштуйте reverse proxy).

### 3. Налаштування

1. Відкрийте `/admin` → залогіньтесь
2. Додайте отримувача (ПІБ, IBAN, ЄДРПОУ/ІПН)
3. Створіть API-ключ
4. Завантажте логотип, налаштуйте кольори
5. Згенеруйте платіжне посилання через API

## API

### POST /generate

Створює платіжне посилання з QR-кодом.

```bash
curl -X POST https://pay.example.com/generate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: vpk_xxxxxxxxxxxxxxxx" \
  -d '{
    "receiver_key": "rcv_abc123",
    "purpose": "За товар HUNTER",
    "amount": "1500"
  }'
```

**Відповідь:**

```json
{
  "pay_url": "https://pay.example.com/p/abc123...",
  "nbu_url": "https://bank.gov.ua/qr/QkNE...",
  "qr_base64": "iVBOR...",
  "expires_in_hours": 24
}
```

| Параметр | Тип | Опис |
|----------|-----|------|
| `receiver_key` | string | Ключ отримувача (з адмінки) |
| `purpose` | string | Призначення платежу (2-420 символів) |
| `amount` | string? | Сума (опціонально, формат `1500` або `1500.00`) |

### GET /p/{link_id}

Платіжна сторінка з QR-кодом, кнопкою оплати та реквізитами.

### GET /health

Статус сервісу (Redis + PostgreSQL).

## Архітектура

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Nginx     │────▶│   FastAPI    │────▶│   Redis     │
│  (proxy)   │     │  (app.py)    │     │  (TTL links) │
└─────────────┘     └──────┬───────┘     └─────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │  PostgreSQL  │
                    │  (config,    │
                    │   receivers, │
                    │   keys, logs)│
                    └──────────────┘
```

| Компонент | Призначення |
|-----------|-------------|
| **FastAPI** | REST API, сторінка оплати, адмінка |
| **Redis** | Зберігання платіжних посилань з TTL |
| **PostgreSQL** | Отримувачі, API-ключі, налаштування брендингу, логи |
| **Docker** | read-only container, tmpfs /tmp, volume /data/static |

## Структура QR-коду (NBU v002)

```
BCD          ← Службова мітка
002          ← Версія формату
2            ← Кодування (Win1251)
UCT          ← Функція (Ukrainian Credit Transfer)
             ← BIC (RFU, порожнє)
ФОП Козарчук ← Отримувач
UA783052...  ← IBAN (29 символів)
UAH1500      ← Сума + валюта
2262003378   ← ЄДРПОУ/ІПН
             ← Ціль (RFU)
             ← Reference (RFU)
За товар     ← Призначення платежу
             ← Відображення (порожнє)
```

Гіперпосилання: `https://bank.gov.ua/qr/{Base64URL(Win1251)}`

## Файли

| Файл | Опис |
|------|------|
| `app.py` | FastAPI: endpoint'и, middleware, логіка |
| `db.py` | PostgreSQL: CRUD, міграції, сесії |
| `templates.py` | HTML-шаблони сторінки оплати |
| `admin.html` | SPA адмін-панель |
| `schema.sql` | DDL: таблиці, індекси, тригери |
| `docker-compose.yml` | Redis + PostgreSQL + App |
| `Dockerfile` | Python 3.12-slim + fonts-dejavu (для ₴ на QR) |
| `ARCHITECTURE.md` | Детальна архітектура v4.0 |
| `SECURITY_AUDIT_v2.md` | Аудит безпеки |

## Налаштування (.env)

| Змінна | Опис | За замовчуванням |
|--------|------|------------------|
| `BASE_URL` | Публічна URL сервісу | `http://localhost:8000` |
| `REDIS_URL` | URL Redis | `redis://redis:6379/0` |
| `DATABASE_URL` | URL PostgreSQL | `postgresql://vilnopay:vilnopay@postgres:5432/vilnopay` |
| `ADMIN_INIT_USER` | Логін адміна (при першому старті) | `admin` |
| `ADMIN_INIT_PASS` | Пароль адміна (при першому старті) | — |
| `SESSION_TTL_HOURS` | Час життя сесії адміна (год) | `8` |
| `ALLOWED_HOSTS` | Дозволені хости | `*` |
| `LOG_LEVEL` | Рівень логування | `INFO` |

## Безпека

- **API-ключі** — SHA-256 хеш, bcrypt сесії
- **CSP** — Content-Security-Policy на всіх сторінках
- **Rate limiting** — slowapi (10 req/хв для /generate, 30 для /p/)
- **Docker** — `read_only: true`, `no-new-privileges`, tmpfs /tmp
- **Знак ₴ на QR** — обов'язковий для v002 за НБУ №97
- **CORS** — відсутній (same-origin only)

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

Приватний проєкт. Використання за погодженням з власником.

---

**Власник:** [HUNTER.rv](https://hunter.rv.ua) · Рівне, Україна