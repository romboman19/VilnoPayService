import base64
import io
import json
import os
import secrets

import qrcode
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="VilnoPayService", version="2.0.0")

API_KEY = os.getenv("API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
LINK_TTL_HOURS = int(os.getenv("LINK_TTL_HOURS", "24"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

rdb = redis.from_url(REDIS_URL, decode_responses=True)


# ── моделі ───────────────────────────────────────────────────────────────────

class QRRequest(BaseModel):
    receiver: str
    iban: str
    code: str
    purpose: str
    amount: str | None = None


class QRResponse(BaseModel):
    pay_url: str
    nbu_url: str
    qr_base64: str
    expires_in_hours: int


# ── бізнес-логіка ────────────────────────────────────────────────────────────

def build_open_data(receiver, iban, code, purpose, amount):
    iban = iban.replace(" ", "").strip()
    amount_line = f"UAH{amount.strip().replace(',', '.')}" if amount else ""
    lines = [
        "BCD", "002", "2", "UCT", "",
        receiver.strip(), iban, amount_line, code.strip(),
        "", "", purpose.strip(), "",
    ]
    return "\n".join(lines) + "\n"


def to_nbu_token(open_data: str) -> str:
    raw = open_data.encode("cp1251", errors="strict")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_qr_png_bytes(url: str) -> bytes:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _check_key(request_key: str | None):
    if API_KEY and request_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        rdb.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "service": "VilnoPayService", "redis": redis_ok}


@app.post("/generate", response_model=QRResponse)
def generate(req: QRRequest, api_key: str | None = None):
    _check_key(api_key)
    try:
        open_data = build_open_data(req.receiver, req.iban, req.code, req.purpose, req.amount)
        nbu_token = to_nbu_token(open_data)
        nbu_url = f"https://bank.gov.ua/qr/{nbu_token}"

        # короткий ID для нашого посилання
        link_id = secrets.token_urlsafe(12)
        pay_url = f"{BASE_URL}/p/{link_id}"

        # зберігаємо дані в Redis з TTL
        payload = {
            "receiver": req.receiver,
            "iban": req.iban,
            "code": req.code,
            "purpose": req.purpose,
            "amount": req.amount or "",
            "nbu_token": nbu_token,
            "nbu_url": nbu_url,
        }
        ttl_seconds = LINK_TTL_HOURS * 3600
        rdb.setex(f"pay:{link_id}", ttl_seconds, json.dumps(payload))

        # QR веде на нашу сторінку вибору
        qr_bytes = generate_qr_png_bytes(pay_url)
        qr_b64 = base64.b64encode(qr_bytes).decode("ascii")

        return QRResponse(
            pay_url=pay_url,
            nbu_url=nbu_url,
            qr_base64=qr_b64,
            expires_in_hours=LINK_TTL_HOURS,
        )
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"Storage error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qr.png")
def qr_png(
    receiver: str,
    iban: str,
    code: str,
    purpose: str,
    amount: str | None = None,
    api_key: str | None = None,
):
    _check_key(api_key)
    # для GET /qr.png генеруємо одноразово без збереження — просто повертаємо PNG
    try:
        open_data = build_open_data(receiver, iban, code, purpose, amount)
        nbu_token = to_nbu_token(open_data)
        nbu_url = f"https://bank.gov.ua/qr/{nbu_token}"
        png_bytes = generate_qr_png_bytes(nbu_url)
        return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/p/{link_id}", response_class=HTMLResponse)
def pay_page(link_id: str):
    raw = rdb.get(f"pay:{link_id}")
    if not raw:
        return HTMLResponse(content=_expired_page(), status_code=410)

    data = json.loads(raw)
    ttl_sec = rdb.ttl(f"pay:{link_id}")
    hours_left = max(1, ttl_sec // 3600)

    nbu_url = data["nbu_url"]
    receiver = data["receiver"]
    iban = data["iban"]
    purpose = data["purpose"]
    amount = data["amount"]
    amount_line = f"{amount} грн" if amount else "за домовленістю"

    # QR для цієї сторінки — веде на nbu_url
    qr_bytes = generate_qr_png_bytes(nbu_url)
    qr_b64 = base64.b64encode(qr_bytes).decode("ascii")

    return HTMLResponse(content=_pay_page_html(
        nbu_url=nbu_url,
        receiver=receiver,
        iban=iban,
        purpose=purpose,
        amount_line=amount_line,
        qr_b64=qr_b64,
        hours_left=hours_left,
    ))


# ── HTML шаблони ──────────────────────────────────────────────────────────────

def _pay_page_html(nbu_url, receiver, iban, purpose, amount_line, qr_b64, hours_left):
    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VilnoPay — Оплата</title>
<style>
  :root {{
    --blue: #2563eb;
    --blue-dark: #1d4ed8;
    --bg: #f1f5f9;
    --card: #ffffff;
    --text: #1e293b;
    --muted: #64748b;
    --border: #e2e8f0;
    --radius: 16px;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 16px; min-height: 100vh; }}
  .wrap {{ max-width: 480px; margin: 0 auto; }}

  /* header */
  .logo {{ text-align: center; padding: 24px 0 20px; }}
  .logo-text {{ font-size: 24px; font-weight: 800; color: var(--text); }}
  .logo-text span {{ color: var(--blue); }}
  .logo-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .expires {{ display: inline-block; margin-top: 8px; font-size: 11px; color: var(--muted); background: #f8fafc; border: 1px solid var(--border); border-radius: 20px; padding: 3px 10px; }}

  /* sections */
  .section {{ background: var(--card); border-radius: var(--radius); padding: 20px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .section-title {{ font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 14px; }}

  /* bank buttons */
  .banks {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .bank-btn {{ display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 12px; border: 1.5px solid var(--border); text-decoration: none; color: var(--text); font-size: 14px; font-weight: 500; background: white; transition: border-color .15s, background .15s; }}
  .bank-btn:hover {{ border-color: var(--blue); background: #eff6ff; }}
  .bank-btn img {{ width: 28px; height: 28px; object-fit: contain; border-radius: 6px; }}
  .bank-btn-other {{ grid-column: 1 / -1; justify-content: center; background: var(--blue); color: white; border-color: var(--blue); font-size: 15px; font-weight: 600; padding: 14px; border-radius: 12px; }}
  .bank-btn-other:hover {{ background: var(--blue-dark); border-color: var(--blue-dark); }}

  /* QR */
  .qr-wrap {{ text-align: center; }}
  .qr-wrap img {{ width: 200px; height: 200px; border-radius: 12px; border: 1px solid var(--border); }}
  .qr-hint {{ font-size: 12px; color: var(--muted); margin-top: 10px; }}

  /* реквізити */
  .req-row {{ display: flex; justify-content: space-between; align-items: flex-start; padding: 10px 0; border-bottom: 1px solid var(--border); gap: 12px; }}
  .req-row:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .req-label {{ font-size: 12px; color: var(--muted); flex-shrink: 0; }}
  .req-value {{ font-size: 14px; font-weight: 500; text-align: right; word-break: break-all; }}
  .copy-btn {{ margin-top: 14px; width: 100%; padding: 13px; border-radius: 12px; border: 1.5px solid var(--border); background: white; font-size: 14px; font-weight: 600; color: var(--blue); cursor: pointer; transition: background .15s; }}
  .copy-btn:hover {{ background: #eff6ff; }}
  .copy-btn.copied {{ color: #16a34a; border-color: #16a34a; }}

  .footer {{ text-align: center; font-size: 11px; color: #cbd5e1; padding: 16px 0 8px; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="logo">
    <div class="logo-text">Vilno<span>Pay</span></div>
    <div class="logo-sub">Безпечна оплата через банківський застосунок</div>
    <div class="expires">⏱ Посилання активне ще {hours_left} год.</div>
  </div>

  <!-- БЛОК 1: Банки -->
  <div class="section">
    <div class="section-title">Оплата через додаток</div>
    <div class="banks">
      <a class="bank-btn" href="{nbu_url}">
        <img src="https://www.privatbank.ua/favicon.ico" onerror="this.style.display='none'" alt="">
        ПриватБанк
      </a>
      <a class="bank-btn" href="{nbu_url}">
        <img src="https://monobank.ua/favicon.ico" onerror="this.style.display='none'" alt="">
        Monobank
      </a>
      <a class="bank-btn" href="{nbu_url}">
        <img src="https://pumb.ua/favicon.ico" onerror="this.style.display='none'" alt="">
        ПУМБ
      </a>
      <a class="bank-btn" href="{nbu_url}">
        <img src="https://sense.com.ua/favicon.ico" onerror="this.style.display='none'" alt="">
        Sense Bank
      </a>
      <a class="bank-btn" href="{nbu_url}">
        <img src="https://abank.com.ua/favicon.ico" onerror="this.style.display='none'" alt="">
        А-Банк
      </a>
      <a class="bank-btn" href="{nbu_url}">
        <img src="https://novapay.ua/favicon.ico" onerror="this.style.display='none'" alt="">
        NovaPay
      </a>
      <a class="bank-btn bank-btn-other" href="{nbu_url}">
        Інший банк →
      </a>
    </div>
  </div>

  <!-- БЛОК 2: QR -->
  <div class="section">
    <div class="section-title">Оплата за QR-кодом</div>
    <div class="qr-wrap">
      <img src="data:image/png;base64,{qr_b64}" alt="QR код для оплати">
      <div class="qr-hint">Відскануйте QR-код із платіжного застосунку вашого банку</div>
    </div>
  </div>

  <!-- БЛОК 3: Реквізити -->
  <div class="section">
    <div class="section-title">Оплата за реквізитами</div>
    <div class="req-row">
      <span class="req-label">Отримувач</span>
      <span class="req-value">{receiver}</span>
    </div>
    <div class="req-row">
      <span class="req-label">IBAN</span>
      <span class="req-value" id="iban-val">{iban}</span>
    </div>
    <div class="req-row">
      <span class="req-label">Призначення</span>
      <span class="req-value">{purpose}</span>
    </div>
    <div class="req-row">
      <span class="req-label">Сума</span>
      <span class="req-value">{amount_line}</span>
    </div>
    <button class="copy-btn" onclick="copyReqs(this)">📋 Скопіювати реквізити</button>
  </div>

  <div class="footer">VilnoPayService</div>
</div>

<script>
function copyReqs(btn) {{
  const text = `Отримувач: {receiver}\\nIBAN: {iban}\\nПризначення: {purpose}\\nСума: {amount_line}`;
  navigator.clipboard.writeText(text).then(() => {{
    btn.textContent = '✅ Скопійовано!';
    btn.classList.add('copied');
    setTimeout(() => {{
      btn.textContent = '📋 Скопіювати реквізити';
      btn.classList.remove('copied');
    }}, 2500);
  }});
}}
</script>
</body>
</html>"""


def _expired_page():
    return """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VilnoPay — Посилання застаріло</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #f1f5f9; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: white; border-radius: 20px; padding: 40px 32px; max-width: 360px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }
  .icon { font-size: 48px; margin-bottom: 16px; }
  h2 { font-size: 20px; color: #1e293b; margin-bottom: 8px; }
  p { font-size: 14px; color: #64748b; line-height: 1.5; }
  .footer { margin-top: 24px; font-size: 11px; color: #cbd5e1; }
</style>
</head>
<body>
<div class="card">
  <div class="icon">⏰</div>
  <h2>Посилання застаріло</h2>
  <p>Термін дії цього платіжного посилання закінчився. Будь ласка, зверніться до продавця для отримання нового посилання.</p>
  <div class="footer">VilnoPayService</div>
</div>
</body>
</html>"""
