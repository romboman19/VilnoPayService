# LiqPay API — Документація для інтеграції

## 1. Checkout API

### Endpoint
```
POST https://www.liqpay.ua/api/3/checkout
```

### Параметри data (JSON → base64)

| Поле | Опис | Приклад |
|------|------|---------|
| `version` | Версія API | `3` |
| `public_key` | Публічний ключ | `i00000000000` (sandbox) |
| `action` | Тип операції | `pay`, `hold`, `subscribe`, `paydonate` |
| `amount` | Сума | `100.00` |
| `currency` | Валюта | `UAH`, `USD`, `EUR` |
| `description` | Опис платежу | `Оплата за товар #12345` |
| `order_id` | Унікальний ID замовлення | `order_12345` |
| `language` | Мова | `uk`, `en` |
| `server_url` | URL для callback | `https://pay.hunter.rv.ua/api/liqpay/callback` |
| `result_url` | URL після оплати | `https://pay.hunter.rv.ua/p/{link_id}?paid=1` |
| `paytypes` | Методи оплати | `card`, `gpay`, `apay`, `qr`, `privat24` |
| `sandbox` | Тестовий режим | `1` |

### Приклад JSON
```json
{
    "version": 3,
    "public_key": "YOUR_PUBLIC_KEY",
    "action": "pay",
    "amount": "100.00",
    "currency": "UAH",
    "description": "Оплата за товар #12345",
    "order_id": "vp_order_abc123",
    "language": "uk",
    "server_url": "https://pay.hunter.rv.ua/api/liqpay/callback",
    "result_url": "https://pay.hunter.rv.ua/p/abc123?paid=1"
}
```

## 2. Signature (підпис)

### Алгоритм
```
signature = base64_encode(sha3-256(private_key + data_base64 + private_key))
```

де `data_base64` = base64(JSON-рядок параметрів)

### Python реалізація
```python
import base64, json, hashlib

def liqpay_signature(private_key, data_b64):
    sign_str = private_key + data_b64 + private_key
    sha = hashlib.sha3_256(sign_str.encode('utf-8')).digest()
    return base64.b64encode(sha).decode('ascii')

def liqpay_data(params: dict):
    json_str = json.dumps(params, ensure_ascii=False, separators=(',', ':'))
    return base64.b64encode(json_str.encode('utf-8')).decode('ascii')
```

## 3. HTML форма (redirect)

```html
<form method="POST" action="https://www.liqpay.ua/api/3/checkout" accept-charset="utf-8">
    <input type="hidden" name="data" value="{DATA_BASE64}" />
    <input type="hidden" name="signature" value="{SIGNATURE}" />
    <input type="submit" value="Оплатити" />
</form>
```

## 4. Widget (JS)

### Підключення
```html
<script src="https://static.liqpay.ua/libjs/checkout.js"></script>
```

### Ініціалізація
```html
<div id="liqpay_checkout"></div>
<script>
    LiqPayCheckout.init({
        data: "{DATA_BASE64}",
        signature: "{SIGNATURE}",
        embedTo: "#liqpay_checkout",
        language: "uk",
        mode: "embed"  // popup | embed
    });
</script>
```

### Події
- `liqpay.ready` — віджет відкрився
- `liqpay.close` — віджет закрився
- `liqpay.callback` — результат оплати

## 5. Callback (server_url)

LiqPay надсилає POST на `server_url` після зміни статусу платежу.

### Формат
```
POST /api/liqpay/callback
Content-Type: application/x-www-form-urlencoded

data={DATA_BASE64}&signature={SIGNATURE}
```

### Розкодування
```python
data_b64 = request.form["data"]
signature = request.form["signature"]
# Верифікація
expected_sig = liqpay_signature(private_key, data_b64)
if signature != expected_sig:
    raise HTTPException(403, "Invalid signature")
# Розкодування data
data = json.loads(base64.b64decode(data_b64))
```

### Поля callback
| Поле | Опис |
|------|------|
| `status` | `success`, `failure`, `wait_accept`, `sandbox` |
| `payment_id` | ID платежу в LiqPay |
| `order_id` | Наш order_id |
| `amount` | Сума |
| `currency` | Валюта |
| `description` | Опис |
| `transaction_id` | ID транзакції |

## 6. Платіжні методи (paytypes)

| Значення | Метод |
|----------|-------|
| `card` | Банківська картка |
| `gpay` | Google Pay |
| `apay` | Apple Pay |
| `qr` | QR-код (Privat24) |
| `privat24` | Privat24 |
| `masterpass` | MasterPass |
| `visaCheckout` | Visa Checkout |

Якщо `paytypes` не вказано — показуються всі доступні методи.

## 7. Sandbox (тестовий режим)

- Тестові ключі: `sandbox_i00000000000` (public), `sandbox_...` (private)
- Додати `"sandbox": 1` у data
- Статус оплати: `sandbox` (не реальний платіж)
- Картка для тестів: `4242 4242 4242 4242`, термін — будь-який майбутній, CVV — будь-який

## 8. Кнопка "Оплатити LiqPay" (простий варіант)

HTML форма з auto-submit:
```html
<form method="POST" action="https://www.liqpay.ua/api/3/checkout" accept-charset="utf-8" id="liqpay-form">
    <input type="hidden" name="data" value="{DATA}" />
    <input type="hidden" name="signature" value="{SIG}" />
</form>
<button onclick="document.getElementById('liqpay-form').submit()">Оплатити LiqPay</button>
```

## 9. Безпека

- **private_key** — зберігати тільки на сервері, ніколи не передавати клієнту
- **Callback verification** — завжди перевіряти signature
- **order_id** — унікальний для кожного платежу
- **server_url** — HTTPS обов'язковий