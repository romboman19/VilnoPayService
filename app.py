"""
VilnoPayService v4.0.0 — Admin Panel + Receiver Keys
"""
import base64, io, json, logging, os, re, secrets, time
from contextlib import asynccontextmanager
from pathlib import Path

import bcrypt
import qrcode
import redis
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
    log_payment_link, cleanup_expired_sessions,
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
        if request.url.path.startswith("/admin"):
            resp.headers["Content-Security-Policy"] = \
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:;"
        else:
            resp.headers["Content-Security-Policy"] = \
                "default-src 'self'; img-src 'self' data: https://*.privatbank.ua; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        return resp

app.add_middleware(SecurityHeadersMiddleware)
if "*" not in ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Helpers ──────────────────────────────────────────────────

def _check_api_key(key: str | None):
    cnt = pg_query("SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = TRUE", fetchone=True)
    if cnt and cnt["cnt"] > 0:
        if not key:
            raise HTTPException(401, "API key required")
        result = validate_api_key(key)
        if not result:
            raise HTTPException(401, "Invalid API key")
        return result
    return {"key_prefix": "none", "label": "no-auth"}

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
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()

def _validate_link_id(lid):
    if not re.match(r"^[A-Za-z0-9_-]{8,32}$", lid):
        raise HTTPException(400, "Invalid link ID")
    return lid


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
        if len(v) < 2 or len(v) > 200: raise ValueError("Отримувач: 2-200 символів")
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
    allowed = {"logo_url","bg_color","primary_color","accent_color",
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

@app.post("/admin/api-keys")
def admin_create_api_key(request: Request, body: ApiKeyCreate):
    _require_admin(request)
    plain_key, record = create_api_key(body.label)
    for k in ("last_used_at", "created_at"):
        if record.get(k): record[k] = str(record[k])
    logger.info("API_KEY_CREATED prefix=%s label=%s", record["key_prefix"], body.label)
    return {"ok": True, "key": plain_key, **record}

@app.delete("/admin/api-keys/{key_id}")
def admin_revoke_api_key(request: Request, key_id: int):
    _require_admin(request)
    revoke_api_key(key_id)
    logger.info("API_KEY_REVOKED id=%s", key_id)
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


# ── Admin HTML Page ──────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
def admin_page(request: Request):
    admin_html_path = Path(__file__).parent / "admin.html"
    if admin_html_path.exists():
        return HTMLResponse(admin_html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>admin.html not found</h1>", status_code=500)


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
    nbu_url = data["nbu_url"]
    amt = data["amount"]
    amt_line = f"{amt} грн" if amt else "за домовленістю"
    qr_b64 = base64.b64encode(generate_qr_png_bytes(nbu_url)).decode("ascii")

    # Брендинг з БД
    settings = get_settings()

    logger.info("LINK_VIEW id=%s ip=%s ttl=%ds", link_id, get_remote_address(request), ttl_sec)
    return HTMLResponse(content=pay_page_html(
        nbu_url, data["receiver"], data["iban"], data["purpose"],
        amt_line, qr_b64, hours_left, settings
    ))
