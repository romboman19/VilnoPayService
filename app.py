"""
VilnoPayService v4.0.0 — Admin Panel + Receiver Keys
"""
import base64, io, json, logging, os, re, secrets, time
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
    create_template, list_templates, delete_template
)
from templates import pay_page_html, expired_page_html

# ── Config ───────────────────────────────────────────────────
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "8"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vilnopay")

rdb = redis.from_url(REDIS_URL, decode_responses=True)


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
            resp.headers["Content-Security-Policy"] = \
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self' https://fonts.gstatic.com"
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

class GenerateResponse(BaseModel):
    pay_url: str; nbu_url: str; qr_base64: str; expires_in_hours: int

class LoginRequest(BaseModel):
    username: str; password: str

class ReceiverCreate(BaseModel):
    name: str; receiver: str; iban: str; edrpou: str
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
    return {"username": session["username"]}


# ── Admin Settings ───────────────────────────────────────────

@app.get("/admin/settings")
def admin_get_settings(request: Request):
    _require_admin(request)
    return get_settings()

@app.put("/admin/settings")
def admin_update_settings(request: Request, body: SettingsUpdate):
    _require_admin(request)
    allowed = {"logo_filename","bg_color","primary_color","accent_color",
               "text_color","card_color","border_color","font_family","font_size",
               "page_title","page_subtitle","footer_text","link_ttl_hours","custom_css"}
    filtered = {k: v for k, v in body.settings.items() if k in allowed}
    update_settings(filtered)
    logger.info("SETTINGS_UPDATED keys=%s", list(filtered.keys()))
    return {"ok": True}


# ── Admin Receivers CRUD ─────────────────────────────────────

@app.get("/admin/receivers")
def admin_list_receivers(request: Request):
    _require_admin(request)
    rows = list_receivers()
    # Серіалізація datetime
    for r in rows:
        for k in ("created_at", "updated_at"):
            if r.get(k):
                r[k] = str(r[k])
    return rows

@app.post("/admin/receivers")
def admin_create_receiver(request: Request, body: ReceiverCreate):
    _require_admin(request)
    rcv = create_receiver(body.name, body.receiver, body.iban, body.edrpou)
    for k in ("created_at", "updated_at"):
        if rcv.get(k): rcv[k] = str(rcv[k])
    logger.info("RECEIVER_CREATED key=%s name=%s", rcv["receiver_key"], body.name)
    return rcv

@app.put("/admin/receivers/{receiver_key}")
def admin_update_receiver(request: Request, receiver_key: str, body: ReceiverUpdate):
    _require_admin(request)
    existing = get_receiver_by_key(receiver_key)
    if not existing:
        raise HTTPException(404, "Отримувач не знайдений")
    rcv = update_receiver(receiver_key, body.name, body.receiver, body.iban, body.edrpou, body.is_active)
    for k in ("created_at", "updated_at"):
        if rcv.get(k): rcv[k] = str(rcv[k])
    return rcv

@app.delete("/admin/receivers/{receiver_key}")
def admin_delete_receiver(request: Request, receiver_key: str):
    _require_admin(request)
    existing = get_receiver_by_key(receiver_key)
    if not existing:
        raise HTTPException(404, "Отримувач не знайдений")
    delete_receiver(receiver_key)
    logger.info("RECEIVER_DELETED key=%s", receiver_key)
    return {"ok": True}


# ── Admin API Keys ───────────────────────────────────────────

@app.get("/admin/api-keys")
def admin_list_api_keys(request: Request):
    _require_admin(request)
    rows = list_api_keys()
    for r in rows:
        for k in ("last_used_at", "created_at"):
            if r.get(k): r[k] = str(r[k])
    return rows

@app.delete("/admin/api-keys/{key_id}")
def admin_revoke_api_key(request: Request, key_id: int):
    _require_admin(request)
    revoke_api_key(key_id)
    logger.info("API_KEY_REVOKED id=%s", key_id)
    return {"ok": True}


# ── Admin Logo Upload ───────────────────────────────────────

@app.post("/admin/upload-logo")
def admin_upload_logo(request: Request, file: UploadFile = File(...)):
    _require_admin(request)
    # Перевірка типу
    if file.content_type not in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"):
        raise HTTPException(400, "Дозволені: PNG, JPEG, WebP, GIF, SVG")
    # Перевірка розміру (max 2MB)
    contents = file.file.read()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(400, "Максимальний розмір: 2MB")
    # Розширення
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
           "image/gif": ".gif", "image/svg+xml": ".svg"}.get(file.content_type, ".png")
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
    _require_admin(request)
    rows = list_page_views(limit)
    for r in rows:
        if r.get("viewed_at"): r["viewed_at"] = str(r["viewed_at"])
    return rows

@app.get("/admin/views-log/{link_id}")
def admin_views_for_link(request: Request, link_id: str):
    _require_admin(request)
    rows = list_page_views_for_link(link_id)
    for r in rows:
        if r.get("viewed_at"): r["viewed_at"] = str(r["viewed_at"])
    return rows


# ── Track bank click ─────────────────────────────────────────

@app.post("/track/bank-click")
@limiter.limit("60/minute")
def track_bank_click(request: Request, body: dict):
    link_id = body.get("link_id", "")
    bank = body.get("bank", "")
    if link_id and bank:
        ua = request.headers.get("user-agent", "")
        device = _detect_device(ua)
        log_page_view(link_id, get_remote_address(request), ua, device, bank_clicked=bank)
    return {"ok": True}


# ── Admin Payment Links Log ─────────────────────────────────

@app.get("/admin/links-log")
def admin_links_log(request: Request, limit: int = 50):
    _require_admin(request)
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
    _require_admin(request)
    return list_managers()

@app.post("/admin/managers")
def admin_create_manager(request: Request, body: dict):
    _require_admin(request)
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
    _require_admin(request)
    delete_manager(manager_id)
    logger.info("MANAGER_DELETED id=%s", manager_id)
    return {"ok": True}

@app.put("/admin/managers/{manager_id}/toggle")
def admin_toggle_manager(request: Request, manager_id: int, body: dict):
    _require_admin(request)
    toggle_manager(manager_id, body.get("is_active", True))
    return {"ok": True}


# ── Manager: Templates ───────────────────────────────────────

@app.get("/manager/templates")
def manager_list_templates(request: Request):
    session = _require_admin(request)
    return list_templates(session["user_id"])

@app.post("/manager/templates")
def manager_create_template(request: Request, body: dict):
    session = _require_admin(request)
    name = body.get("name", "").strip()
    receiver_key = body.get("receiver_key", "").strip()
    purpose = body.get("purpose", "").strip()
    default_amount = body.get("default_amount", "").strip() or None
    if not name or not receiver_key or not purpose:
        raise HTTPException(400, "Назва, отримувач і призначення обов'язкові")
    return create_template(session["user_id"], name, receiver_key, purpose, default_amount)

@app.delete("/manager/templates/{template_id}")
def manager_delete_template(request: Request, template_id: int):
    session = _require_admin(request)
    delete_template(template_id, session["user_id"])
    return {"ok": True}


# ── Manager: Create payment ─────────────────────────────────

@app.post("/manager/create-payment")
def manager_create_payment(request: Request, body: dict):
    session = _require_admin(request)
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
    payload = {
        "receiver_key": receiver_key,
        "receiver": rcv["receiver"], "iban": rcv["iban"], "code": rcv["edrpou"],
        "purpose": purpose, "amount": amount or "",
        "nbu_token": nbu_token, "nbu_url": nbu_url,
        "created_at": int(time.time()),
        "created_ip": get_remote_address(request)
    }
    rdb.setex(f"pay:{link_id}", ttl * 3600, json.dumps(payload))
    # Логувати з prefix менеджерського ключа
    mgr_key = pg_query("SELECT key_prefix FROM api_keys WHERE label = %s AND is_active = TRUE LIMIT 1",
                       (f"manager_{session.get('username','')}",), fetchone=True)
    key_prefix = mgr_key["key_prefix"] if mgr_key else "mgr"
    log_payment_link(link_id, receiver_key, purpose, amount, key_prefix, get_remote_address(request))
    qr_b64 = base64.b64encode(generate_qr_png_bytes(pay_url)).decode("ascii")
    logger.info("MANAGER_PAYMENT id=%s manager=%s rcv=%s", link_id, session.get("username"), receiver_key)
    return {"pay_url": pay_url, "nbu_url": nbu_url, "qr_base64": qr_b64, "link_id": link_id}


# ── Manager: History ────────────────────────────────────────

@app.get("/manager/history")
def manager_history(request: Request, limit: int = 50):
    session = _require_admin(request)
    from db import list_manager_payments
    return list_manager_payments(session.get("username", ""), limit)


# ── Manager: Receivers list ─────────────────────────────────

@app.get("/manager/receivers")
def manager_receivers(request: Request):
    _require_admin(request)
    return list_receivers()



# ── Manager HTML ────────────────────────────────────────────

@app.get("/manager", response_class=HTMLResponse)
@app.get("/manager/", response_class=HTMLResponse)
def manager_page(request: Request):
    manager_html_path = Path(__file__).parent / "manager.html"
    if manager_html_path.exists():
        return HTMLResponse(manager_html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>manager.html not found</h1>", status_code=500)


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
    _require_admin(request)
    settings = get_settings()
    logo_fn = settings.get("logo_filename", "")
    logo_url = f"/static/{logo_fn}" if logo_fn else ""
    qr_b64 = base64.b64encode(generate_qr_png_bytes("https://bank.gov.ua/qr/test")).decode("ascii")
    return HTMLResponse(content=pay_page_html(
        "https://bank.gov.ua/qr/test",
        "ФОП Тестовий Тест Тестович",
        "UA783052990000026005012107358",
        "За товар (тестове посилання)",
        "1 500 ₴", qr_b64, 23, settings, logo_url, "preview", "2262003378", 23*3600
    ))


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
        rdb.setex(f"pay:{link_id}", ttl * 3600, json.dumps(payload))

        # Аудит-лог
        log_payment_link(link_id, req.receiver_key, req.purpose,
                         req.amount, api_info.get("key_prefix", ""), get_remote_address(request))

        qr_b64 = base64.b64encode(generate_qr_png_bytes(pay_url)).decode("ascii")
        logger.info("LINK_CREATED id=%s rcv_key=%s amt=%s ip=%s",
                     link_id, req.receiver_key, req.amount or "-", get_remote_address(request))
        return GenerateResponse(pay_url=pay_url, nbu_url=nbu_url,
                                qr_base64=qr_b64, expires_in_hours=ttl)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Generate error")
        raise HTTPException(500, "Внутрішня помилка")


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

    return HTMLResponse(content=pay_page_html(
        nbu_url, data["receiver"], data["iban"], data["purpose"],
        amt_line, qr_b64, hours_left, settings, logo_url, link_id, data.get("code", ""), ttl_seconds
    ))
