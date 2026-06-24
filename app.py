"""
VilnoPayService v3.0.0 — Secure Edition
Виправлені: XSS, API key leakage, rate limiting,
input validation, error handling, logging, security headers.
"""
import base64, io, json, logging, os, re, secrets, time
from contextlib import asynccontextmanager
import qrcode, redis
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from markupsafe import escape as html_escape
import html as html_lib
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

API_KEY = os.getenv("API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
LINK_TTL_HOURS = int(os.getenv("LINK_TTL_HOURS", "24"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vilnopay")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        rdb.ping()
        logger.info("Redis connected: %s", REDIS_URL.split("@")[-1])
    except redis.RedisError as e:
        logger.error("Redis connection failed: %s", e)
        raise SystemExit(1)
    yield

app = FastAPI(title="VilnoPayService", version="3.0.0",
              docs_url=None, redoc_url=None, lifespan=lifespan)
rdb = redis.from_url(REDIS_URL, decode_responses=True)

limiter = Limiter(key_func=get_remote_address, storage_uri=REDIS_URL,
                  default_limits=["100/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: https://*.privatbank.ua https://monobank.ua "
            "https://pumb.ua https://sense.com.ua https://abank.com.ua https://novapay.ua; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        return response

app.add_middleware(SecurityHeadersMiddleware)
if "*" not in ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# ── моделі з валідацією ──────────────────────────────────────────────────────
class QRRequest(BaseModel):
    receiver: str; iban: str; code: str; purpose: str
    amount: str | None = None

    @field_validator("iban")
    @classmethod
    def validate_iban(cls, v):
        v = v.replace(" ", "").strip().upper()
        if not re.match(r"^UA\d{27}$", v):
            raise ValueError("IBAN: UA + 27 цифр")
        return v

    @field_validator("receiver")
    @classmethod
    def validate_receiver(cls, v):
        v = v.strip()
        if len(v) < 2 or len(v) > 200: raise ValueError("Отримувач: 2-200 символів")
        if re.search(r"[<>]", v): raise ValueError("Заборонені символи")
        return v

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, v):
        v = v.strip()
        if len(v) < 2 or len(v) > 420: raise ValueError("Призначення: 2-420 символів")
        if re.search(r"[<>]", v): raise ValueError("Заборонені символи")
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        if v is None or v.strip() == "": return None
        v = v.strip().replace(",", ".")
        if not re.match(r"^\d{1,10}(\.\d{1,2})?$", v): raise ValueError("Невірний формат суми")
        if float(v) <= 0: raise ValueError("Сума > 0")
        return v

    @field_validator("code")
    @classmethod
    def validate_code(cls, v):
        v = v.strip()
        if not re.match(r"^\d{5,10}$", v): raise ValueError("ЄДРПОУ/ІПН: 5-10 цифр")
        return v

class QRResponse(BaseModel):
    pay_url: str; nbu_url: str; qr_base64: str; expires_in_hours: int

# ── бізнес-логіка ────────────────────────────────────────────────────────────
def build_open_data(receiver, iban, code, purpose, amount):
    iban = iban.replace(" ", "").strip()
    amount_line = f"UAH{amount.strip().replace(',', '.')}" if amount else ""
    return "\n".join(["BCD","002","2","UCT","",receiver.strip(),iban,
                      amount_line,code.strip(),"","",purpose.strip(),""]) + "\n"

def to_nbu_token(open_data):
    raw = open_data.encode("cp1251", errors="strict")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def generate_qr_png_bytes(url):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(url); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()

def _check_key(request_key):
    if not API_KEY: return
    if not request_key: raise HTTPException(401, "API key required")
    if not secrets.compare_digest(request_key, API_KEY):
        raise HTTPException(401, "Invalid API key")

def _validate_link_id(link_id):
    if not re.match(r"^[A-Za-z0-9_-]{8,32}$", link_id):
        raise HTTPException(400, "Invalid link ID")
    return link_id

def _js_escape(s):
    return (s.replace("\\","\\\\").replace("'","\\'").replace('"','\\"')
             .replace("\n","\\n").replace("\r","\\r")
             .replace("<","\\x3c").replace(">","\\x3e").replace("&","\\x26"))

# ── endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
@limiter.limit("30/minute")
def health(request: Request):
    try: rdb.ping(); redis_ok = True
    except Exception: redis_ok = False
    return {"status": "ok", "service": "VilnoPayService", "redis": redis_ok}

@app.post("/generate", response_model=QRResponse)
@limiter.limit("10/minute")
def generate(request: Request, req: QRRequest,
             x_api_key: str | None = Header(None, alias="X-API-Key")):
    _check_key(x_api_key)
    try:
        open_data = build_open_data(req.receiver, req.iban, req.code, req.purpose, req.amount)
        nbu_token = to_nbu_token(open_data)
        nbu_url = f"https://bank.gov.ua/qr/{nbu_token}"
        link_id = secrets.token_urlsafe(12)
        pay_url = f"{BASE_URL}/p/{link_id}"
        payload = {"receiver": req.receiver, "iban": req.iban, "code": req.code,
                   "purpose": req.purpose, "amount": req.amount or "",
                   "nbu_token": nbu_token, "nbu_url": nbu_url,
                   "created_at": int(time.time()), "created_ip": get_remote_address(request)}
        rdb.setex(f"pay:{link_id}", LINK_TTL_HOURS * 3600, json.dumps(payload))
        qr_b64 = base64.b64encode(generate_qr_png_bytes(pay_url)).decode("ascii")
        logger.info("LINK_CREATED id=%s iban=%s...%s amt=%s ip=%s",
                     link_id, req.iban[:6], req.iban[-4:], req.amount or "-",
                     get_remote_address(request))
        return QRResponse(pay_url=pay_url, nbu_url=nbu_url,
                          qr_base64=qr_b64, expires_in_hours=LINK_TTL_HOURS)
    except redis.RedisError:
        logger.exception("Redis error"); raise HTTPException(503, "Тимчасова помилка")
    except HTTPException: raise
    except Exception:
        logger.exception("Generate error"); raise HTTPException(500, "Внутрішня помилка")

@app.get("/qr.png")
@limiter.limit("20/minute")
def qr_png(request: Request, receiver: str, iban: str, code: str, purpose: str,
           amount: str | None = None,
           x_api_key: str | None = Header(None, alias="X-API-Key")):
    _check_key(x_api_key)
    try:
        od = build_open_data(receiver, iban, code, purpose, amount)
        nbu_url = f"https://bank.gov.ua/qr/{to_nbu_token(od)}"
        return StreamingResponse(io.BytesIO(generate_qr_png_bytes(nbu_url)), media_type="image/png")
    except Exception:
        logger.exception("QR error"); raise HTTPException(500, "Помилка генерації QR")

@app.get("/p/{link_id}", response_class=HTMLResponse)
@limiter.limit("30/minute")
def pay_page(request: Request, link_id: str):
    link_id = _validate_link_id(link_id)
    raw = rdb.get(f"pay:{link_id}")
    if not raw:
        logger.info("LINK_MISS id=%s ip=%s", link_id, get_remote_address(request))
        return HTMLResponse(content=_expired_page(), status_code=410)
    data = json.loads(raw)
    ttl_sec = rdb.ttl(f"pay:{link_id}")
    hours_left = max(1, ttl_sec // 3600)
    nbu_url = data["nbu_url"]
    amt = data["amount"]
    amt_line = f"{amt} грн" if amt else "за домовленістю"
    qr_b64 = base64.b64encode(generate_qr_png_bytes(nbu_url)).decode("ascii")
    logger.info("LINK_VIEW id=%s ip=%s ttl=%ds", link_id, get_remote_address(request), ttl_sec)
    return HTMLResponse(content=_pay_page_html(nbu_url, data["receiver"], data["iban"],
                                           data["purpose"], amt_line, qr_b64, hours_left))

# ── HTML шаблони (імпорт з окремого файлу для чистоти) ────────────────────────


def _pay_page_html(nbu_url, receiver, iban, purpose, amount_line, qr_b64, hours_left):
    import html as _h
    receiver = _h.escape(str(receiver))
    iban = _h.escape(str(iban))
    purpose = _h.escape(str(purpose))
    amount_line = _h.escape(str(amount_line))
    nbu_url = _h.escape(str(nbu_url))
    # JSON-безпечні значення для JS
    import json as _j
    _js_data = _j.dumps({"receiver": str(_h.unescape(receiver)), "iban": str(_h.unescape(iban)),
                         "purpose": str(_h.unescape(purpose)), "amount": str(_h.unescape(amount_line))})
    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#2563eb" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0f172a" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>VilnoPay — Оплата</title>
<style>
:root {{
  --blue:#2563eb; --blue-dk:#1d4ed8; --blue-lt:#eff6ff; --blue-bd:#bfdbfe;
  --green:#16a34a; --green-lt:#f0fdf4; --green-bd:#bbf7d0;
  --amber:#f59e0b;
  --bg:#f1f5f9; --card:#fff; --text:#0f172a; --text2:#334155;
  --muted:#64748b; --border:#e2e8f0;
  --sh:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
  --r:18px; --rs:12px; --t:.17s cubic-bezier(.4,0,.2,1);
}}
@media(prefers-color-scheme:dark){{
  :root{{
    --bg:#0f172a; --card:#1e293b; --text:#f1f5f9; --text2:#cbd5e1;
    --muted:#94a3b8; --border:#334155;
    --blue-lt:#172554; --blue-bd:#1e40af;
    --green-lt:#052e16; --green-bd:#14532d;
    --sh:0 1px 3px rgba(0,0,0,.3),0 4px 12px rgba(0,0,0,.2);
  }}
}}
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
  background:var(--bg);color:var(--text);
  min-height:100svh;
  padding:0 0 env(safe-area-inset-bottom,24px);
  -webkit-tap-highlight-color:transparent;
  overscroll-behavior:none;
}}
.wrap{{max-width:480px;margin:0 auto;padding:8px 14px 32px}}

/* Header */
.logo{{text-align:center;padding:18px 0 14px}}
.logo-mark{{font-size:24px;font-weight:800;letter-spacing:-.5px;color:var(--text)}}
.logo-mark em{{color:var(--blue);font-style:normal}}
.logo-sub{{font-size:12px;color:var(--muted);margin-top:4px}}
.badge-ttl{{
  display:inline-flex;align-items:center;gap:5px;
  margin-top:10px;font-size:11px;color:var(--muted);
  background:var(--card);border:1px solid var(--border);
  border-radius:99px;padding:3px 10px 3px 8px;
}}
.dot{{width:6px;height:6px;border-radius:50%;background:var(--amber);flex-shrink:0;
  animation:pulse-dot 2.2s ease-in-out infinite}}
@keyframes pulse-dot{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.4;transform:scale(.7)}}}}

/* Секція */
.section{{
  background:var(--card);border-radius:var(--r);
  padding:16px 16px 18px;margin-bottom:10px;
  box-shadow:var(--sh);border:1px solid var(--border);
  animation:fade-up .35s var(--t) both;
}}
.section:nth-child(2){{animation-delay:.04s}}
.section:nth-child(3){{animation-delay:.09s}}
.section:nth-child(4){{animation-delay:.14s}}
@keyframes fade-up{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:none}}}}
.sec-head{{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;
  letter-spacing:.08em;margin-bottom:12px}}

/* Сума */
.amount-chip{{
  display:inline-flex;align-items:center;gap:6px;
  background:var(--blue-lt);border:1px solid var(--blue-bd);
  color:var(--blue);font-size:21px;font-weight:800;
  border-radius:var(--rs);padding:6px 14px;margin-bottom:14px;
  letter-spacing:-.5px;
}}

/* Банки */
.banks{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.bank-btn{{
  display:flex;align-items:center;gap:9px;
  padding:11px 12px;border-radius:var(--rs);
  border:1.5px solid var(--border);text-decoration:none;
  color:var(--text);font-size:13.5px;font-weight:600;
  background:var(--card);
  transition:border-color var(--t),background var(--t),transform var(--t);
  -webkit-user-select:none;user-select:none;
  position:relative;overflow:hidden;
}}
.bank-btn:active{{transform:scale(.95);border-color:var(--blue)}}
.bank-icon{{
  width:28px;height:28px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;
  font-size:18px;flex-shrink:0;background:#f1f5f9;
}}
@media(prefers-color-scheme:dark){{.bank-icon{{background:#334155}}}}
.bank-wide{{
  grid-column:1/-1;justify-content:center;
  background:var(--blue);color:#fff;border-color:var(--blue);
  font-size:14.5px;padding:13px;
}}
.bank-wide:active{{background:var(--blue-dk);border-color:var(--blue-dk)}}
.bank-wide .bank-icon{{background:rgba(255,255,255,.15);font-size:16px}}

/* QR */
.qr-wrap{{text-align:center;padding:4px 0 2px}}
.qr-img{{
  width:min(260px,85vw);height:min(260px,85vw);
  border-radius:14px;border:1px solid var(--border);
  display:block;margin:0 auto;background:#fff;
}}
.qr-hint{{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.4}}

/* Реквізити */
.req-row{{
  display:flex;justify-content:space-between;align-items:center;
  padding:10px 0;border-bottom:1px solid var(--border);
  gap:8px;
}}
.req-row:last-of-type{{border-bottom:none;padding-bottom:0}}
.req-left{{flex:1;min-width:0}}
.req-label{{font-size:11px;color:var(--muted);font-weight:500;margin-bottom:2px}}
.req-value{{font-size:14px;font-weight:600;color:var(--text);word-break:break-all;line-height:1.35}}
.req-value.mono{{font-family:"SF Mono","Fira Code",ui-monospace,monospace;font-size:13px;letter-spacing:.01em}}

/* Кнопка копіювання для поля */
.copy-field{{
  flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  width:36px;height:36px;border-radius:10px;
  border:1.5px solid var(--border);
  background:var(--card);cursor:pointer;
  font-size:16px;
  transition:border-color var(--t),background var(--t),transform var(--t);
  position:relative;
}}
.copy-field:active{{transform:scale(.86)}}
.copy-field.ok{{border-color:var(--green);background:var(--green-lt)}}
.copy-field .tip{{
  position:absolute;bottom:calc(100% + 6px);left:50%;
  transform:translateX(-50%) scale(.8);
  background:#1e293b;color:#fff;
  font-size:11px;font-weight:600;
  border-radius:6px;padding:3px 8px;
  white-space:nowrap;pointer-events:none;
  opacity:0;transition:opacity var(--t),transform var(--t);
}}
.copy-field.ok .tip{{opacity:1;transform:translateX(-50%) scale(1)}}
@media(prefers-color-scheme:dark){{.copy-field .tip{{background:#f1f5f9;color:#0f172a}}}}

/* Кнопка "Скопіювати всі" */
.copy-all{{
  display:flex;align-items:center;justify-content:center;gap:8px;
  width:100%;margin-top:14px;padding:13px;
  border-radius:var(--rs);border:1.5px solid var(--border);
  background:var(--card);font-size:14px;font-weight:700;
  color:var(--blue);cursor:pointer;
  transition:background var(--t),border-color var(--t),color var(--t),transform var(--t);
}}
.copy-all:active{{transform:scale(.97)}}
.copy-all.ok{{color:var(--green);border-color:var(--green);background:var(--green-lt)}}

/* Футер */
.footer{{text-align:center;font-size:10px;color:var(--muted);opacity:.4;padding:14px 0 4px}}

/* Ripple */
@keyframes ripple{{to{{transform:scale(4);opacity:0}}}}
.rpl{{
  position:absolute;border-radius:50%;
  background:rgba(37,99,235,.12);
  width:40px;height:40px;margin:-20px;
  pointer-events:none;
  animation:ripple .55s linear forwards;
}}
</style>
</head>
<body>
<div class="wrap">

  <!-- Шапка -->
  <div class="logo">
    <div class="logo-mark">Vilno<em>Pay</em></div>
    <div class="logo-sub">Безпечна оплата через банківський застосунок</div>
    <div class="badge-ttl">
      <span class="dot"></span>
      Посилання активне ще {hours_left} год.
    </div>
  </div>

  <!-- БЛОК 1: Оплата через додаток -->
  <div class="section">
    <div class="sec-head">Оплата через додаток</div>
    <div class="amount-chip">💳 {amount_line}</div>
    <div class="banks">
      <a class="bank-btn" href="{nbu_url}" target="_blank" rel="noopener" onclick="return openBank(event,'privatbank')">
        <span class="bank-icon">🟢</span>ПриватБанк
      </a>
      <a class="bank-btn" href="{nbu_url}" target="_blank" rel="noopener" onclick="return openBank(event,'monobank')">
        <span class="bank-icon">🖤</span>Monobank
      </a>
      <a class="bank-btn" href="{nbu_url}" target="_blank" rel="noopener" onclick="return openBank(event,'pumb')">
        <span class="bank-icon">🔴</span>ПУМБ
      </a>
      <a class="bank-btn" href="{nbu_url}" target="_blank" rel="noopener" onclick="return openBank(event,'sense')">
        <span class="bank-icon">🔵</span>Sense Bank
      </a>
      <a class="bank-btn" href="{nbu_url}" target="_blank" rel="noopener" onclick="return openBank(event,'abank')">
        <span class="bank-icon">🟡</span>А-Банк
      </a>
      <a class="bank-btn" href="{nbu_url}" target="_blank" rel="noopener" onclick="return openBank(event,'novapay')">
        <span class="bank-icon">🟠</span>NovaPay
      </a>
      <a class="bank-btn bank-wide" href="{nbu_url}" target="_blank" rel="noopener" onclick="addRipple(event)">
        <span class="bank-icon">🏦</span>Відкрити у будь-якому банку →
      </a>
    </div>
  </div>

  <!-- БЛОК 2: QR -->
  <div class="section">
    <div class="sec-head">Сканувати QR-код</div>
    <div class="qr-wrap">
      <img class="qr-img" src="data:image/png;base64,{qr_b64}" alt="QR код для оплати" loading="eager">
      <div class="qr-hint">Відскануйте з мобільного застосунку вашого банку</div>
    </div>
  </div>

  <!-- БЛОК 3: Реквізити -->
  <div class="section">
    <div class="sec-head">Реквізити для оплати</div>

    <div class="req-row">
      <div class="req-left">
        <div class="req-label">Отримувач</div>
        <div class="req-value" id="v-receiver">{receiver}</div>
      </div>
      <button class="copy-field" onclick="copyField(this,'v-receiver')" aria-label="Копіювати отримувача">
        📋<span class="tip">Скопійовано!</span>
      </button>
    </div>

    <div class="req-row">
      <div class="req-left">
        <div class="req-label">IBAN</div>
        <div class="req-value mono" id="v-iban">{iban}</div>
      </div>
      <button class="copy-field" onclick="copyField(this,'v-iban')" aria-label="Копіювати IBAN">
        📋<span class="tip">Скопійовано!</span>
      </button>
    </div>

    <div class="req-row">
      <div class="req-left">
        <div class="req-label">Призначення платежу</div>
        <div class="req-value" id="v-purpose">{purpose}</div>
      </div>
      <button class="copy-field" onclick="copyField(this,'v-purpose')" aria-label="Копіювати призначення">
        📋<span class="tip">Скопійовано!</span>
      </button>
    </div>

    <div class="req-row">
      <div class="req-left">
        <div class="req-label">Сума</div>
        <div class="req-value" id="v-amount">{amount_line}</div>
      </div>
      <button class="copy-field" onclick="copyField(this,'v-amount')" aria-label="Копіювати суму">
        📋<span class="tip">Скопійовано!</span>
      </button>
    </div>

    <button class="copy-all" id="btn-copy-all" onclick="copyAll(this)">
      📋 Скопіювати всі реквізити
    </button>
  </div>

  <div class="footer">VilnoPayService · Захищено</div>
</div>

<script>
// Копіювання одного поля
function copyField(btn, fieldId) {{
  const text = document.getElementById(fieldId).textContent.trim();
  navigator.clipboard.writeText(text).then(() => {{
    btn.classList.add('ok');
    btn.innerHTML = '✅<span class="tip">Скопійовано!</span>';
    setTimeout(() => {{
      btn.classList.remove('ok');
      btn.innerHTML = '📋<span class="tip">Скопійовано!</span>';
    }}, 2200);
  }}).catch(() => {{
    // fallback для старих браузерів
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    btn.classList.add('ok');
    btn.innerHTML = '✅<span class="tip">Скопійовано!</span>';
    setTimeout(() => {{
      btn.classList.remove('ok');
      btn.innerHTML = '📋<span class="tip">Скопійовано!</span>';
    }}, 2200);
  }});
}}

// Копіювання всіх реквізитів
function copyAll(btn) {{
  const receiver = document.getElementById('v-receiver').textContent.trim();
  const iban     = document.getElementById('v-iban').textContent.trim();
  const purpose  = document.getElementById('v-purpose').textContent.trim();
  const amount   = document.getElementById('v-amount').textContent.trim();
  const text = `Отримувач: ${{receiver}}\nIBAN: ${{iban}}\nПризначення: ${{purpose}}\nСума: ${{amount}}`;
  navigator.clipboard.writeText(text).then(() => showCopiedAll(btn))
    .catch(() => {{
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      showCopiedAll(btn);
    }});
}}
function showCopiedAll(btn) {{
  btn.classList.add('ok');
  btn.textContent = '✅ Скопійовано!';
  setTimeout(() => {{
    btn.classList.remove('ok');
    btn.textContent = '📋 Скопіювати всі реквізити';
  }}, 2500);
}}

// Ripple effect для банківських кнопок
function addRipple(e) {{
  const btn = e.currentTarget;
  const r = document.createElement('span');
  r.className = 'rpl';
  const rect = btn.getBoundingClientRect();
  r.style.left = (e.clientX - rect.left) + 'px';
  r.style.top  = (e.clientY - rect.top) + 'px';
  btn.appendChild(r);
  r.addEventListener('animationend', () => r.remove());
}}
// Відкрити конкретний банк-додаток
// Спробуємо deep link, якщо не вийшло за 1.5с — fallback на NBU URL
function openBank(e, bank) {{
  addRipple(e);
  // Deep links для кожного банку
  var deepLinks = {{
    'privatbank': 'privatbank://',
    'monobank': 'monobank://',
    'pumb': 'pumbonline://',
    'sense': 'sensebank://',
    'abank': 'abankua://',
    'novapay': 'novapay://'
  }};
  var deep = deepLinks[bank];
  if (!deep) return true; // fallback на NBU URL
  
  var start = Date.now();
  var iframe = document.createElement('iframe');
  iframe.style.display = 'none';
  iframe.src = deep;
  document.body.appendChild(iframe);
  
  // Якщо додаток не відкрився за 1.5с — йдемо на NBU URL
  setTimeout(function() {{
    document.body.removeChild(iframe);
    if (Date.now() - start < 1600) {{
      // Додаток не відкрився — відкриваємо NBU URL
      window.location.href = '{nbu_url}';
    }}
  }}, 1500);
  
  return false; // не відкриваємо NBU URL одразу
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
