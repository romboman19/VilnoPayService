"""
VilnoPayService v4.0.0 — Admin Panel + Receiver Keys
"""
import base64, hashlib, io, json, logging, os, re, secrets, time
from contextlib import asynccontextmanager
from pathlib import Path

import bcrypt
import qrcode
import redis
from fastapi import FastAPI, Header, HTTPException, Request, Response, UploadFile, File
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from db import (
    init_db, pg_query, pg_execute,
    get_settings, update_settings, get_link_ttl,
    validate_api_key, create_api_key, list_api_keys, revoke_api_key,
    create_receiver, get_receiver_by_key, list_receivers,
    update_receiver, delete_receiver,
    create_admin_session, get_admin_session, delete_admin_session,
    log_payment_link, log_page_view, list_page_views, list_page_views_for_link,
    cleanup_expired_sessions,
    create_manager, list_managers, delete_manager, toggle_manager,
    create_template, list_templates, delete_template,
    # Payment providers
    create_provider, get_provider_by_type, get_active_providers, list_providers,
    update_provider, delete_provider, get_provider_decrypted,
    create_liqpay_tx, update_liqpay_tx, get_liqpay_tx_by_link, list_liqpay_transactions,
    get_receiver_liqpay_private
)
from templates import pay_page_html, expired_page_html, liqpay_result_html

# ── Config ───────────────────────────────────────────────────
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "8"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vilnopay")

rdb = redis.from_url(REDIS_URL, decode_responses=True)



def _cleanup_stale_invoices():
    """Видалити PDF файли для яких немає Redis мета-запису."""
    invoice_dir = Path("/data/invoices")
    if not invoice_dir.exists():
        return
    for pdf_file in invoice_dir.glob("*.pdf"):
        invoice_id = pdf_file.stem
        if not rdb.exists(f"invoice_meta:{invoice_id}"):
            pdf_file.unlink(missing_ok=True)
            logger.info("STARTUP_CLEANUP removed stale invoice %s", invoice_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        rdb.ping(); logger.info("Redis OK")
    except redis.RedisError as e:
        logger.error("Redis failed: %s", e); raise SystemExit(1)
    try:
        init_db(); logger.info("PostgreSQL OK")
    except Exception as e:
        logger.error("PostgreSQL failed: %s", e); raise SystemExit(1)
    _cleanup_stale_invoices()
    yield

app = FastAPI(title="VilnoPayService", version="4.0.0",
              docs_url=None, redoc_url=None, lifespan=lifespan)
limiter = Limiter(key_func=get_remote_address, storage_uri=REDIS_URL,
                  default_limits=["100/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/admin") or request.url.path.startswith("/manager"):
            resp.headers["Content-Security-Policy"] = \
                "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self' https://fonts.gstatic.com"
        else:
            # Публічнi сторiнки — дозволити LiqPay
            csp = ("default-src 'self'; "
                   "img-src 'self' data:; "
                   "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                   "script-src 'self' 'unsafe-inline' https://static.liqpay.ua https://static.cloudflareinsights.com; "
                   "frame-src 'self' https://www.liqpay.ua https://static.liqpay.ua; "
                   "connect-src 'self' https://www.liqpay.ua https://www.cloudflareinsights.com; "
                   "font-src 'self' https://fonts.gstatic.com")
            resp.headers["Content-Security-Policy"] = csp
        return resp

app.add_middleware(SecurityHeadersMiddleware)
if "*" not in ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

static_dir = Path("/data/static")
try:
    static_dir.mkdir(parents=True, exist_ok=True)
except Exception:
    pass  # Директорія створена в Dockerfile
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Helpers ──────────────────────────────────────────────────

def _check_api_key(key: str | None):
    if not key or not key.startswith("vpk_"):
        raise HTTPException(401, "API key required (vpk_...)")
    result = validate_api_key(key)
    if not result:
        raise HTTPException(401, "Invalid API key")
    return result

def _require_admin(request: Request) -> dict:
    token = request.cookies.get("session_token")
    session = get_admin_session(token)
    if not session:
        raise HTTPException(401, "Unauthorized")
    return session

def _require_manager(request: Request) -> dict:
    session = _require_admin(request)
    if session.get("role") != "manager":
        raise HTTPException(403, "Доступ лише для менеджерів")
    return session

def _require_role(request: Request, role: str) -> dict:
    session = _require_admin(request)
    if session.get("role") != role:
        raise HTTPException(403, "Недостатньо прав")
    return session

def build_open_data(receiver, iban, code, purpose, amount):
    iban = iban.replace(" ", "").strip()
    amt = f"UAH{amount.strip().replace(',', '.')}" if amount else ""
    return "\n".join(["BCD","002","2","UCT","",receiver.strip(),iban,
                      amt,code.strip(),"","",purpose.strip(),""]) + "\n"

def to_nbu_token(open_data):
    raw = open_data.encode("cp1251", errors="strict")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def generate_qr_png_bytes(url):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(url); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    # Додаємо знак гривні ₴ у фоновому колі (вимога НБУ №97 для версії 002)
    img = _add_hryvnia_sign(img)
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()

def _add_hryvnia_sign(qr_img):
    """Додає знак ₴ у білому колі в центрі QR-коду (вимога НБУ)."""
    from PIL import Image, ImageDraw, ImageFont
    img = qr_img.convert("RGBA")
    w, h = img.size
    # Розмір фонового кола ~30% від сторони QR
    circle_d = int(w * 0.28)
    # Створюємо overlay
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Біле коло
    cx, cy = w // 2, h // 2
    draw.ellipse([cx - circle_d // 2, cy - circle_d // 2,
                  cx + circle_d // 2, cy + circle_d // 2],
                 fill=(255, 255, 255, 255))
    img = Image.alpha_composite(img, overlay)
    # Текст ₴
    draw = ImageDraw.Draw(img)
    font_size = int(circle_d * 0.55)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()
    # Центруємо текст
    text = "\u20b4"  # знак гривні
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = cx - tw // 2 - bbox[0]
    ty = cy - th // 2 - bbox[1]
    draw.text((tx, ty), text, fill=(0, 0, 0, 255), font=font)
    return img.convert("RGB")

def _validate_link_id(lid):
    if not re.match(r"^[A-Za-z0-9_-]{8,32}$", lid):
        raise HTTPException(400, "Invalid link ID")
    return lid

def _detect_device(ua: str) -> str:
    ua = (ua or "").lower()
    if "ipad" in ua or "tablet" in ua: return "tablet"
    if "iphone" in ua or "android" in ua or "mobile" in ua: return "mobile"
    return "desktop"


# ── Pydantic models ──────────────────────────────────────────

class GenerateRequest(BaseModel):
    receiver_key: str; purpose: str; amount: str | None = None
    invoice_id: str | None = None
    invoice_url: str | None = None

    @field_validator("receiver_key")
    @classmethod
    def val_rk(cls, v):
        v = v.strip()
        if not re.match(r"^rcv_[A-Za-z0-9]{4,40}$", v):
            raise ValueError("Невірний receiver_key")
        return v

    @field_validator("purpose")
    @classmethod
    def val_p(cls, v):
        v = v.strip()
        if len(v) < 2 or len(v) > 420: raise ValueError("Призначення: 2-420 символів")
        if re.search(r"[<>]", v): raise ValueError("Заборонені символи")
        return v

    @field_validator("amount")
    @classmethod
    def val_a(cls, v):
        if v is None or v.strip() == "": return None
        v = v.strip().replace(",", ".")
        if not re.match(r"^\d{1,10}(\.\d{1,2})?$", v): raise ValueError("Невірний формат суми")
        if float(v) <= 0: raise ValueError("Сума > 0")
        return v

    @field_validator("invoice_id")
    @classmethod
    def val_inv_id(cls, v):
        if v is None: return None
        v = v.strip()
        if not re.match(r"^[A-Za-z0-9_-]{16,32}$", v):
            raise ValueError("Невірний invoice_id")
        return v

    @field_validator("invoice_url")
    @classmethod
    def val_inv_url(cls, v):
        if v is None or v.strip() == "": return None
        v = v.strip()
        if len(v) > 500: raise ValueError("URL занадто довгий (max 500)")
        if not re.match(r"^https://", v): raise ValueError("invoice_url має починатись з https://")
        return v

class GenerateResponse(BaseModel):
    pay_url: str; nbu_url: str; qr_base64: str; expires_in_hours: int
    invoice_url: str | None = None

class LoginRequest(BaseModel):
    username: str; password: str

class ReceiverCreate(BaseModel):
    name: str; receiver: str; iban: str; edrpou: str
    liqpay_public_key: str = ""
    liqpay_private_key: str = ""
    liqpay_display_mode: str = ""
    liqpay_pay_methods: str = '["card","privat24","wallet"]'
    liqpay_sandbox: bool = False
    @field_validator("iban")
    @classmethod
    def val_iban(cls, v):
        v = v.replace(" ", "").strip().upper()
        if not re.match(r"^UA\d{27}$", v): raise ValueError("IBAN: UA + 27 цифр")
        return v
    @field_validator("receiver")
    @classmethod
    def val_rcv(cls, v):
        v = v.strip()
        if len(v) < 2 or len(v) > 140: raise ValueError("Отримувач: 2-140 символів")
        return v
    @field_validator("edrpou")
    @classmethod
    def val_edr(cls, v):
        v = v.strip()
        if not re.match(r"^\d{5,10}$", v): raise ValueError("ЄДРПОУ: 5-10 цифр")
        return v

class ReceiverUpdate(ReceiverCreate):
    is_active: bool = True

class SettingsUpdate(BaseModel):
    settings: dict[str, str]

class ApiKeyCreate(BaseModel):
    label: str = "default"


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.get("/health")
@limiter.limit("30/minute")
def health(request: Request):
    r_ok = p_ok = False
    try: rdb.ping(); r_ok = True
    except: pass
    try: pg_query("SELECT 1", fetchone=True); p_ok = True
    except: pass
    return {"status": "ok" if r_ok and p_ok else "degraded", "redis": r_ok, "postgres": p_ok}


# ── Admin Auth ───────────────────────────────────────────────

@app.post("/admin/login")
@limiter.limit("5/minute")
def admin_login(request: Request, body: LoginRequest, response: Response):
    user = pg_query(
        "SELECT id, username, password_hash FROM admin_users WHERE username = %s AND is_active = TRUE",
        (body.username,), fetchone=True
    )
    if not user or not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        raise HTTPException(401, "Невірний логін або пароль")
    token = create_admin_session(user["id"], get_remote_address(request),
                                  request.headers.get("user-agent", ""), SESSION_TTL_HOURS)
    response = JSONResponse({"ok": True, "username": user["username"]})
    response.set_cookie("session_token", token, httponly=True, samesite="strict",
                        max_age=SESSION_TTL_HOURS * 3600, secure=True)
    logger.info("ADMIN_LOGIN user=%s ip=%s", user["username"], get_remote_address(request))
    return response

@app.post("/admin/logout")
def admin_logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        delete_admin_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_token")
    return response

@app.get("/admin/me")
def admin_me(request: Request):
    session = _require_admin(request)
    return {"username": session["username"], "role": session.get("role", "admin")}


# ── Admin Settings ───────────────────────────────────────────

@app.get("/admin/settings")
def admin_get_settings(request: Request):
    _require_role(request, "admin")
    return get_settings()

@app.put("/admin/settings")
def admin_update_settings(request: Request, body: SettingsUpdate):
    _require_role(request, "admin")
    allowed = {"logo_filename","bg_color","primary_color","accent_color",
               "text_color","card_color","border_color","font_family","font_size",
               "page_title","page_subtitle","footer_text","link_ttl_hours","custom_css","block_order"}
    filtered = {k: v for k, v in body.settings.items() if k in allowed}
    if "custom_css" in filtered:
        css = filtered["custom_css"]
        if len(css) > 4000:
            raise HTTPException(400, "custom_css: максимум 4000 символів")
        forbidden = ["url(", "expression(", "import", "@charset", "javascript:"]
        if any(f in css.lower() for f in forbidden):
            raise HTTPException(400, "custom_css: заборонені конструкції")
    update_settings(filtered)
        # Валідація block_order
    if "block_order" in filtered:
        try:
            order = json.loads(filtered["block_order"])
            valid_blocks = {"nbu_qr", "liqpay", "requisites"}
            if not isinstance(order, list) or not all(b in valid_blocks for b in order):
                raise HTTPException(400, "block_order: невірний формат")
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(400, "block_order: невалідний JSON")
    logger.info("SETTINGS_UPDATED keys=%s", list(filtered.keys()))
    return {"ok": True}


# ── Admin Receivers CRUD ─────────────────────────────────────

@app.get("/admin/receivers")
def admin_list_receivers(request: Request):
    _require_role(request, "admin")
    rows = list_receivers()
    # Серіалізація datetime
    for r in rows:
        for k in ("created_at", "updated_at"):
            if r.get(k):
                r[k] = str(r[k])
    return rows

@app.post("/admin/receivers")
def admin_create_receiver(request: Request, body: ReceiverCreate):
    _require_role(request, "admin")
    rcv = create_receiver(body.name, body.receiver, body.iban, body.edrpou,
                      liqpay_public_key=body.liqpay_public_key,
                      liqpay_private_key=body.liqpay_private_key,
                      liqpay_display_mode=body.liqpay_display_mode,
                      liqpay_pay_methods=body.liqpay_pay_methods,
                      liqpay_sandbox=body.liqpay_sandbox)
    for k in ("created_at", "updated_at"):
        if rcv.get(k): rcv[k] = str(rcv[k])
    logger.info("RECEIVER_CREATED key=%s name=%s", rcv["receiver_key"], body.name)
    return rcv

@app.put("/admin/receivers/{receiver_key}")
def admin_update_receiver(request: Request, receiver_key: str, body: ReceiverUpdate):
    _require_role(request, "admin")
    existing = get_receiver_by_key(receiver_key)
    if not existing:
        raise HTTPException(404, "Отримувач не знайдений")
    update_receiver(receiver_key,
        name=body.name, receiver=body.receiver, iban=body.iban, edrpou=body.edrpou,
        is_active=body.is_active,
        liqpay_public_key=body.liqpay_public_key,
        liqpay_private_key=body.liqpay_private_key,
        liqpay_display_mode=body.liqpay_display_mode,
        liqpay_pay_methods=body.liqpay_pay_methods,
        liqpay_sandbox=body.liqpay_sandbox
    )
    rcv = get_receiver_by_key(receiver_key)
    for k in ("created_at", "updated_at"):
        if rcv.get(k): rcv[k] = str(rcv[k])
    return rcv

@app.delete("/admin/receivers/{receiver_key}")
def admin_delete_receiver(request: Request, receiver_key: str):
    _require_role(request, "admin")
    existing = get_receiver_by_key(receiver_key)
    if not existing:
        raise HTTPException(404, "Отримувач не знайдений")
    delete_receiver(receiver_key)
    logger.info("RECEIVER_DELETED key=%s", receiver_key)
    return {"ok": True}


# ── Admin API Keys ───────────────────────────────────────────

@app.get("/admin/api-keys")
def admin_list_api_keys(request: Request):
    _require_role(request, "admin")
    rows = list_api_keys()
    for r in rows:
        for k in ("last_used_at", "created_at"):
            if r.get(k): r[k] = str(r[k])
    return rows

@app.delete("/admin/api-keys/{key_id}")
def admin_revoke_api_key(request: Request, key_id: int):
    _require_role(request, "admin")
    revoke_api_key(key_id)
    logger.info("API_KEY_REVOKED id=%s", key_id)
    return {"ok": True}


# ── Admin Logo Upload ───────────────────────────────────────

@app.post("/admin/upload-logo")
def admin_upload_logo(request: Request, file: UploadFile = File(...)):
    _require_role(request, "admin")
    # Валідація типу (SVG заборонено — XSS ризик)
    ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    if file.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(400, "Дозволені формати: PNG, JPEG, WebP, GIF")
    # Перевірка розміру (max 2MB)
    contents = file.file.read()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(400, "Максимальний розмір: 2MB")
    # Magic bytes перевірка
    MAGIC = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg",
             b"RIFF": "image/webp", b"GIF8": "image/gif"}
    if not any(contents.startswith(sig) for sig in MAGIC):
        raise HTTPException(400, "Файл не відповідає задекларованому типу")
    # Розширення
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
           "image/gif": ".gif"}.get(file.content_type, ".png")
    filename = f"logo{ext}"
    logo_path = static_dir / filename
    logo_path.write_bytes(contents)
    # Зберегти ім'я файлу в settings
    update_settings({"logo_filename": filename})
    logger.info("LOGO_UPLOADED file=%s size=%d", filename, len(contents))
    return {"ok": True, "logo_url": f"/static/{filename}"}


# ── Admin Views Log ──────────────────────────────────────────

@app.get("/admin/views-log")
def admin_views_log(request: Request, limit: int = 100):
    _require_role(request, "admin")
    rows = list_page_views(limit)
    for r in rows:
        if r.get("viewed_at"): r["viewed_at"] = str(r["viewed_at"])
    return rows

@app.get("/admin/views-log/{link_id}")
def admin_views_for_link(request: Request, link_id: str):
    _require_role(request, "admin")
    rows = list_page_views_for_link(link_id)
    for r in rows:
        if r.get("viewed_at"): r["viewed_at"] = str(r["viewed_at"])
    return rows


# ── Track bank click ─────────────────────────────────────────

@app.post("/track/bank-click")
@limiter.limit("60/minute")
def track_bank_click(request: Request, body: dict):
    ALLOWED_BANKS = {
        "privat", "mono", "oschad", "ukrsib", "pumb",
        "raiffeisen", "ukrgas", "credit_agricole", "sportbank",
        "izibank", "sense", "other",
        "share_success", "download_fallback", "share_error", "universal"
    }
    link_id = body.get("link_id", "").strip()
    bank = body.get("bank", "").strip().lower()
    if not link_id or not re.match(r"^[A-Za-z0-9_-]{8,32}$", link_id):
        return {"ok": False}
    if bank not in ALLOWED_BANKS:
        return {"ok": False}
    ua = request.headers.get("user-agent", "")
    device = _detect_device(ua)
    log_page_view(link_id, get_remote_address(request), ua, device, bank_clicked=bank)
    return {"ok": True}


# ── Admin Payment Links Log ─────────────────────────────────

@app.get("/admin/links-log")
def admin_links_log(request: Request, limit: int = 50):
    _require_role(request, "admin")
    rows = pg_query(
        "SELECT * FROM payment_links_log ORDER BY created_at DESC LIMIT %s",
        (min(limit, 200),), fetchall=True
    ) or []
    for r in rows:
        if r.get("created_at"): r["created_at"] = str(r["created_at"])
    return rows




# ── Admin: Managers ─────────────────────────────────────────

@app.get("/admin/managers")
def admin_list_managers(request: Request):
    _require_role(request, "admin")
    return list_managers()

@app.post("/admin/managers")
def admin_create_manager(request: Request, body: dict):
    _require_role(request, "admin")
    username = body.get("username", "").strip()
    password = body.get("password", "")
    name = body.get("name", "").strip()
    if not username or not password:
        raise HTTPException(400, "Логін і пароль обов'язкові")
    if len(password) < 6:
        raise HTTPException(400, "Пароль мінімум 6 символів")
    existing = pg_query("SELECT id FROM admin_users WHERE username = %s", (username,), fetchone=True)
    if existing:
        raise HTTPException(400, "Користувач вже існує")
    mgr = create_manager(username, password, name)
    logger.info("MANAGER_CREATED user=%s", username)
    return mgr or {"ok": True}

@app.delete("/admin/managers/{manager_id}")
def admin_delete_manager(request: Request, manager_id: int):
    _require_role(request, "admin")
    delete_manager(manager_id)
    logger.info("MANAGER_DELETED id=%s", manager_id)
    return {"ok": True}

@app.put("/admin/managers/{manager_id}/toggle")
def admin_toggle_manager(request: Request, manager_id: int, body: dict):
    _require_role(request, "admin")
    toggle_manager(manager_id, body.get("is_active", True))
    return {"ok": True}


# ── Manager: Templates ───────────────────────────────────────

@app.get("/manager/templates")
def manager_list_templates(request: Request):
    session = _require_manager(request)
    return list_templates(session["user_id"])

@app.post("/manager/templates")
def manager_create_template(request: Request, body: dict):
    session = _require_manager(request)
    name = body.get("name", "").strip()
    receiver_key = body.get("receiver_key", "").strip()
    purpose = body.get("purpose", "").strip()
    default_amount = body.get("default_amount", "").strip() or None
    if not name or not receiver_key or not purpose:
        raise HTTPException(400, "Назва, отримувач і призначення обов'язкові")
    return create_template(session["user_id"], name, receiver_key, purpose, default_amount)

@app.delete("/manager/templates/{template_id}")
def manager_delete_template(request: Request, template_id: int):
    session = _require_manager(request)
    delete_template(template_id, session["user_id"])
    return {"ok": True}


# ── Manager: Create payment ─────────────────────────────────

@app.post("/manager/create-payment")
def manager_create_payment(request: Request, body: dict):
    session = _require_manager(request)
    receiver_key = body.get("receiver_key", "").strip()
    purpose = body.get("purpose", "").strip()
    amount = body.get("amount", "").strip() or None
    if not receiver_key or not purpose:
        raise HTTPException(400, "Отримувач і призначення обов'язкові")
    # Використовуємо існуючу логіку generate
    rcv = get_receiver_by_key(receiver_key)
    if not rcv or not rcv["is_active"]:
        raise HTTPException(404, "Отримувач не знайдений або неактивний")
    open_data = build_open_data(rcv["receiver"], rcv["iban"], rcv["edrpou"], purpose, amount)
    nbu_token = to_nbu_token(open_data)
    nbu_url = f"https://bank.gov.ua/qr/{nbu_token}"
    link_id = secrets.token_urlsafe(12)
    pay_url = f"{BASE_URL}/p/{link_id}"
    ttl = get_link_ttl()
    invoice_id = body.get("invoice_id", "").strip() or None
    invoice_url = body.get("invoice_url", "").strip() or None
    payload = {
        "receiver_key": receiver_key,
        "receiver": rcv["receiver"], "iban": rcv["iban"], "code": rcv["edrpou"],
        "purpose": purpose, "amount": amount or "",
        "nbu_token": nbu_token, "nbu_url": nbu_url,
        "created_at": int(time.time()),
        "created_ip": get_remote_address(request)
    }
    # Invoice support
    if invoice_id:
        if not rdb.exists(f"invoice_meta:{invoice_id}"):
            raise HTTPException(400, "invoice_id не знайдено або вже застарів")
        payload["invoice_id"] = invoice_id
        rdb.expire(f"invoice_meta:{invoice_id}", ttl * 3600)
    if invoice_url:
        payload["invoice_url"] = invoice_url
    rdb.setex(f"pay:{link_id}", ttl * 3600, json.dumps(payload))
    # Логувати з prefix менеджерського ключа
    mgr_key = pg_query("SELECT key_prefix FROM api_keys WHERE label = %s AND is_active = TRUE LIMIT 1",
                       (f"manager_{session.get('username','')}",), fetchone=True)
    key_prefix = mgr_key["key_prefix"] if mgr_key else "mgr"
    log_payment_link(link_id, receiver_key, purpose, amount, key_prefix, get_remote_address(request))
    qr_b64 = base64.b64encode(generate_qr_png_bytes(pay_url)).decode("ascii")
    logger.info("MANAGER_PAYMENT id=%s manager=%s rcv=%s", link_id, session.get("username"), receiver_key)
    # Invoice URL for response
    response_invoice_url = None
    if invoice_id:
        response_invoice_url = f"{BASE_URL}/invoice/{link_id}"
    elif invoice_url:
        response_invoice_url = invoice_url
    return {"pay_url": pay_url, "nbu_url": nbu_url, "qr_base64": qr_b64, "link_id": link_id,
            "invoice_url": response_invoice_url}


# ── Manager: History ────────────────────────────────────────

@app.get("/manager/history")
def manager_history(request: Request, limit: int = 50):
    session = _require_manager(request)
    from db import list_manager_payments
    return list_manager_payments(session.get("username", ""), limit)


# ── Manager: Receivers list ─────────────────────────────────

@app.get("/manager/receivers")
def manager_receivers(request: Request):
    _require_manager(request)
    return list_receivers()



# ── Manager HTML ────────────────────────────────────────────

@app.get("/manager", response_class=HTMLResponse)
@app.get("/manager/", response_class=HTMLResponse)
def manager_page(request: Request):
    # HTML сторiнка без авторизацiї — JS перевiряє логiн
    manager_html_path = Path(__file__).parent / "manager.html"
    if manager_html_path.exists():
        return HTMLResponse(manager_html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>manager.html not found</h1>", status_code=500)



@app.post("/admin/cleanup-invoices")
def admin_cleanup_invoices(request: Request):
    _require_role(request, "admin")
    invoice_dir = Path("/data/invoices")
    if not invoice_dir.exists():
        return {"deleted": 0}
    deleted = 0
    for pdf_file in invoice_dir.glob("*.pdf"):
        invoice_id = pdf_file.stem
        if not rdb.exists(f"invoice_meta:{invoice_id}"):
            pdf_file.unlink(missing_ok=True)
            deleted += 1
    logger.info("CLEANUP_INVOICES deleted=%d", deleted)
    return {"deleted": deleted}



# ── LiqPay Helpers ────────────────────────────────────────────

def liqpay_encode_data(params: dict) -> str:
    """base64(JSON) для LiqPay API v3."""
    return base64.b64encode(json.dumps(params, ensure_ascii=False).encode()).decode()


def liqpay_signature(private_key: str, data: str) -> str:
    """signature = base64(sha1(private_key + data + private_key)).
    LiqPay v3 використовує SHA-1."""
    sign_str = private_key + data + private_key
    return base64.b64encode(hashlib.sha1(sign_str.encode()).digest()).decode()


def liqpay_verify_callback(private_key: str, data: str, signature: str) -> bool:
    return liqpay_signature(private_key, data, signature) == signature


# ── Public LiqPay Endpoints ───────────────────────────────────

@app.get("/liqpay/checkout-data/{link_id}")
@limiter.limit("30/minute")
def liqpay_checkout_data(request: Request, link_id: str):
    """Генерація data + signature для LiqPay віджета/кнопки."""
    link_id = _validate_link_id(link_id)
    raw = rdb.get(f"pay:{link_id}")
    if not raw:
        raise HTTPException(410, "Посилання неактивне")
    pay_data = json.loads(raw)
    amount = pay_data.get("amount")
    if not amount:
        raise HTTPException(400, "Сума не вказана — LiqPay потребує суму")
    # LiqPay налаштовується на отримувача
    receiver_key = pay_data.get("receiver_key", "")
    rcv = get_receiver_by_key(receiver_key) if receiver_key else None
    if not rcv or not rcv.get("liqpay_public_key") or not rcv.get("liqpay_display_mode"):
        raise HTTPException(404, "LiqPay не налаштований для цього отримувача")
    private_key = get_receiver_liqpay_private(receiver_key)
    if not private_key:
        raise HTTPException(500, "Ключі LiqPay не налаштовані")
    lp = rcv  # використовуємо дані отримувача
    order_id = f"vp_{link_id}_{secrets.token_urlsafe(6)}"
    create_liqpay_tx(link_id, order_id, 0)  # provider_id=0 (сирота, бо прив'язка до отримувача)
    lp_params = {
        "version": 3,
        "public_key": lp["liqpay_public_key"],
        "action": "pay",
        "amount": float(amount),
        "currency": "UAH",
        "description": pay_data.get("purpose", "Оплата"),
        "order_id": order_id,
        "language": "uk",
        "server_url": f"{BASE_URL}/liqpay/callback",
        "result_url": f"{BASE_URL}/liqpay/result/{link_id}",
    }
    if lp.get("liqpay_sandbox"):
        lp_params["sandbox"] = 1
    try:
        methods = json.loads(lp.get("liqpay_pay_methods", "[]"))
        if methods:
            lp_params["paytypes"] = ",".join(methods)
    except (json.JSONDecodeError, TypeError):
        pass
    data_b64 = liqpay_encode_data(lp_params)
    signature = liqpay_signature(private_key, data_b64)
    logger.info("LIQPAY_CHECKOUT link=%s order=%s amount=%s", link_id, order_id, amount)
    return {
        "data": data_b64,
        "signature": signature,
        "public_key": lp["liqpay_public_key"],
        "display_mode": lp["liqpay_display_mode"],
        "is_sandbox": lp.get("liqpay_sandbox", False)
    }


@app.post("/liqpay/callback")
async def liqpay_callback(request: Request):
    """Server-to-server callback від LiqPay."""
    form = await request.form()
    data = form.get("data", "")
    signature = form.get("signature", "")
    if not data or not signature:
        logger.warning("LIQPAY_CALLBACK empty data/signature ip=%s", get_remote_address(request))
        raise HTTPException(400, "Missing data or signature")
    try:
        decoded = json.loads(base64.b64decode(data).decode())
    except Exception:
        raise HTTPException(400, "Invalid data")
    order_id = decoded.get("order_id", "")
    if not order_id:
        raise HTTPException(400, "Missing order_id")
    tx = pg_query("SELECT provider_id, link_id FROM liqpay_transactions WHERE order_id=%s",
                  (order_id,), fetchone=True)
    if not tx:
        logger.warning("LIQPAY_CALLBACK unknown order=%s", order_id)
        raise HTTPException(404, "Unknown order_id")
    # Знайти отримувача через link_id
    pay_raw = rdb.get(f"pay:{tx['link_id']}")
    if not pay_raw:
        raise HTTPException(404, "Payment link not found")
    pay_info = json.loads(pay_raw)
    receiver_key = pay_info.get("receiver_key", "")
    private_key = get_receiver_liqpay_private(receiver_key)
    if not private_key:
        raise HTTPException(500, "Provider key error")
    if not liqpay_verify_callback(private_key, data, signature):
        logger.warning("LIQPAY_CALLBACK invalid signature order=%s ip=%s",
                       order_id, get_remote_address(request))
        raise HTTPException(403, "Invalid signature")
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
    if status in ("success", "sandbox"):
        link_id_part = order_id.split("_")[1] if "_" in order_id else ""
        if link_id_part:
            rdb.setex(f"liqpay_paid:{link_id_part}", 86400,
                      json.dumps({"status": status, "amount": decoded.get("amount")}))
    return {"ok": True}


@app.get("/liqpay/result/{link_id}", response_class=HTMLResponse)
@limiter.limit("30/minute")
def liqpay_result(request: Request, link_id: str):
    """Сторінка результату після повернення з LiqPay."""
    link_id = _validate_link_id(link_id)
    tx = get_liqpay_tx_by_link(link_id)
    settings = get_settings()
    logo_fn = settings.get("logo_filename", "")
    logo_url = f"/static/{logo_fn}" if logo_fn else ""
    return HTMLResponse(liqpay_result_html(tx, settings, logo_url))


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


# ── Admin HTML Page ──────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
def admin_page(request: Request):
    admin_html_path = Path(__file__).parent / "admin.html"
    if admin_html_path.exists():
        return HTMLResponse(admin_html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>admin.html not found</h1>", status_code=500)


@app.get("/admin/preview", response_class=HTMLResponse)
def admin_preview(request: Request):
    _require_role(request, "admin")
    settings = get_settings()
    logo_fn = settings.get("logo_filename", "")
    logo_url = f"/static/{logo_fn}" if logo_fn else ""
    qr_b64 = base64.b64encode(generate_qr_png_bytes("https://bank.gov.ua/qr/test")).decode("ascii")
    return HTMLResponse(content=pay_page_html(
        "https://bank.gov.ua/qr/test",
        "ФОП Тестовий Тест Тестович",
        "UA783052990000026005012107358",
        "За товар (тестове посилання)",
        "1 500 ₴", qr_b64, 23, settings, logo_url, "preview", "2262003378", 23*3600, None
    ))



# ══════════════════════════════════════════════════════════════
# PUBLIC: /upload-invoice
# ══════════════════════════════════════════════════════════════

@app.post("/upload-invoice")
@limiter.limit("5/minute")
def upload_invoice(request: Request, file: UploadFile = File(...),
                   x_api_key: str | None = Header(None, alias="X-API-Key")):
    # Приймати або API ключ, або менеджерську сесію
    if x_api_key and x_api_key.startswith("vpk_"):
        _check_api_key(x_api_key)
    else:
        _require_manager(request)
    if file.content_type != "application/pdf":
        raise HTTPException(400, "Дозволено тільки PDF")
    contents = file.file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(400, "Максимальний розмір PDF: 5MB")
    if not contents.startswith(b"%PDF-"):
        raise HTTPException(400, "Файл не є валідним PDF")
    invoice_id = secrets.token_urlsafe(16)
    invoice_dir = Path("/data/invoices")
    try:
        invoice_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass  # volume або Dockerfile створив
    invoice_path = invoice_dir / f"{invoice_id}.pdf"
    invoice_path.write_bytes(contents)
    ttl = get_link_ttl()
    rdb.setex(f"invoice_meta:{invoice_id}", ttl * 3600,
              json.dumps({"original_name": (file.filename or "invoice.pdf")[:200],
                          "size": len(contents), "created_at": int(time.time())}))
    logger.info("INVOICE_UPLOADED id=%s size=%d ip=%s", invoice_id, len(contents), get_remote_address(request))
    return {"invoice_id": invoice_id, "expires_in_hours": ttl}


# ══════════════════════════════════════════════════════════════
# PUBLIC: /generate
# ══════════════════════════════════════════════════════════════

@app.post("/generate", response_model=GenerateResponse)
@limiter.limit("10/minute")
def generate(request: Request, req: GenerateRequest,
             x_api_key: str | None = Header(None, alias="X-API-Key")):
    api_info = _check_api_key(x_api_key)
    try:
        # Знайти отримувача за ключем
        rcv = get_receiver_by_key(req.receiver_key)
        if not rcv or not rcv["is_active"]:
            raise HTTPException(404, "Отримувач не знайдений або неактивний")

        receiver = rcv["receiver"]
        iban = rcv["iban"]
        code = rcv["edrpou"]

        open_data = build_open_data(receiver, iban, code, req.purpose, req.amount)
        nbu_token = to_nbu_token(open_data)
        nbu_url = f"https://bank.gov.ua/qr/{nbu_token}"
        link_id = secrets.token_urlsafe(12)
        pay_url = f"{BASE_URL}/p/{link_id}"
        ttl = get_link_ttl()

        payload = {
            "receiver_key": req.receiver_key,
            "receiver": receiver, "iban": iban, "code": code,
            "purpose": req.purpose, "amount": req.amount or "",
            "nbu_token": nbu_token, "nbu_url": nbu_url,
            "created_at": int(time.time()),
            "created_ip": get_remote_address(request)
        }
        # Invoice support
        if req.invoice_id:
            if not rdb.exists(f"invoice_meta:{req.invoice_id}"):
                raise HTTPException(400, "invoice_id не знайдено або вже застарів")
            payload["invoice_id"] = req.invoice_id
            rdb.expire(f"invoice_meta:{req.invoice_id}", ttl * 3600)
        if req.invoice_url:
            payload["invoice_url"] = req.invoice_url
        rdb.setex(f"pay:{link_id}", ttl * 3600, json.dumps(payload))

        # Аудит-лог
        log_payment_link(link_id, req.receiver_key, req.purpose,
                         req.amount, api_info.get("key_prefix", ""), get_remote_address(request))

        qr_b64 = base64.b64encode(generate_qr_png_bytes(pay_url)).decode("ascii")
        logger.info("LINK_CREATED id=%s rcv_key=%s amt=%s ip=%s",
                     link_id, req.receiver_key, req.amount or "-", get_remote_address(request))
        # Invoice URL for response
        response_invoice_url = None
        if req.invoice_id:
            response_invoice_url = f"{BASE_URL}/invoice/{link_id}"
        elif req.invoice_url:
            response_invoice_url = req.invoice_url
        return GenerateResponse(pay_url=pay_url, nbu_url=nbu_url,
                                qr_base64=qr_b64, expires_in_hours=ttl,
                                invoice_url=response_invoice_url)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Generate error")
        raise HTTPException(500, "Внутрішня помилка")



# ══════════════════════════════════════════════════════════════
# PUBLIC: /invoice/{link_id}
# ══════════════════════════════════════════════════════════════

@app.get("/invoice/{link_id}")
@limiter.limit("20/minute")
def download_invoice(request: Request, link_id: str):
    link_id = _validate_link_id(link_id)
    raw = rdb.get(f"pay:{link_id}")
    if not raw:
        raise HTTPException(410, "Посилання вже неактивне")
    data = json.loads(raw)
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        raise HTTPException(404, "До цього посилання не прикріплено рахунок")
    meta_raw = rdb.get(f"invoice_meta:{invoice_id}")
    if not meta_raw:
        raise HTTPException(410, "Рахунок-фактура недоступна")
    meta = json.loads(meta_raw)
    invoice_path = Path("/data/invoices") / f"{invoice_id}.pdf"
    if not invoice_path.exists():
        raise HTTPException(404, "Файл рахунку не знайдено")
    # ASCII-only filename for header (latin-1 safe)
    import urllib.parse
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", meta.get("original_name", "invoice.pdf"))
    if not safe_name or safe_name == ".":
        safe_name = "invoice.pdf"
    encoded_name = urllib.parse.quote(meta.get("original_name", "invoice.pdf"), safe="")
    return FileResponse(path=str(invoice_path), media_type="application/pdf",
                        filename=safe_name,
                        headers={"Content-Disposition": f"attachment; filename=\"{safe_name}\"; filename*=UTF-8''{encoded_name}"})


# ══════════════════════════════════════════════════════════════
# PUBLIC: /p/{link_id}
# ══════════════════════════════════════════════════════════════

@app.get("/p/{link_id}", response_class=HTMLResponse)
@limiter.limit("30/minute")
def pay_page(request: Request, link_id: str):
    link_id = _validate_link_id(link_id)
    raw = rdb.get(f"pay:{link_id}")
    if not raw:
        logger.info("LINK_MISS id=%s ip=%s", link_id, get_remote_address(request))
        return HTMLResponse(content=expired_page_html(), status_code=410)

    data = json.loads(raw)
    ttl_sec = rdb.ttl(f"pay:{link_id}")
    hours_left = max(1, ttl_sec // 3600)
    ttl_seconds = ttl_sec
    nbu_url = data["nbu_url"]
    amt = data["amount"]
    amt_line = f"{amt} грн" if amt else "за домовленістю"
    qr_b64 = base64.b64encode(generate_qr_png_bytes(nbu_url)).decode("ascii")

    # Брендинг з БД
    settings = get_settings()

    # Лог перегляду
    ua = request.headers.get("user-agent", "")
    device = _detect_device(ua)
    log_page_view(link_id, get_remote_address(request), ua, device)
    logger.info("LINK_VIEW id=%s ip=%s device=%s ttl=%ds", link_id, get_remote_address(request), device, ttl_sec)

    # Логотип
    logo_fn = settings.get("logo_filename", "")
    logo_url = f"/static/{logo_fn}" if logo_fn else ""
    logger.info("LOGO_CHECK fn=%s url=%s file_exists=%s", logo_fn, logo_url, (static_dir / logo_fn).exists() if logo_fn else False)

    # Перевірити чи у отримувача налаштований LiqPay
    receiver_key = data.get("receiver_key", "")
    rcv_data = get_receiver_by_key(receiver_key) if receiver_key else None
    active_providers = []
    if rcv_data and rcv_data.get("liqpay_public_key") and rcv_data.get("liqpay_display_mode"):
        active_providers = [{"provider_type": "liqpay", "display_mode": rcv_data.get("liqpay_display_mode",""), "public_key": rcv_data.get("liqpay_public_key",""), "pay_methods": rcv_data.get("liqpay_pay_methods","[]"), "is_sandbox": rcv_data.get("liqpay_sandbox",False)}]
    block_order_raw = settings.get("block_order", '["nbu_qr","liqpay","requisites"]')
    try:
        block_order = json.loads(block_order_raw)
    except (json.JSONDecodeError, TypeError):
        block_order = ["nbu_qr", "liqpay", "requisites"]

    # Перевірити чи вже оплачено через LiqPay
    liqpay_paid_raw = rdb.get(f"liqpay_paid:{link_id}")
    liqpay_paid = json.loads(liqpay_paid_raw) if liqpay_paid_raw else None

    return HTMLResponse(content=pay_page_html(
        nbu_url, data["receiver"], data["iban"], data["purpose"],
        amt_line, qr_b64, hours_left, settings, logo_url, link_id, data.get("code", ""), ttl_seconds,
        data.get("invoice_url") or (f"{BASE_URL}/invoice/{link_id}" if data.get("invoice_id") else None),
        providers=active_providers,
        block_order=block_order,
        liqpay_paid=liqpay_paid
    ))
