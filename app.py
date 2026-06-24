import base64
import io
import os
import qrcode
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="VilnoPayService", version="1.0.0")

API_KEY = os.getenv("API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


class QRRequest(BaseModel):
    receiver: str
    iban: str
    code: str
    purpose: str
    amount: str | None = None


class QRResponse(BaseModel):
    url: str
    pay_url: str
    qr_base64: str


def build_open_data(receiver, iban, code, purpose, amount):
    iban = iban.replace(" ", "").strip()
    amount_line = f"UAH{amount.strip().replace(',', '.')}" if amount else ""
    lines = [
        "BCD", "002", "2", "UCT", "",
        receiver.strip(), iban, amount_line, code.strip(),
        "", "", purpose.strip(), "",
    ]
    return "\n".join(lines) + "\n"


def to_nbu_url(open_data):
    raw = open_data.encode("cp1251", errors="strict")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"https://bank.gov.ua/qr/{token}", token


def generate_qr_png_bytes(url):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _check_key(request_key):
    if API_KEY and request_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health():
    return {"status": "ok", "service": "VilnoPayService"}


@app.post("/generate", response_model=QRResponse)
def generate(req: QRRequest, api_key: str | None = None):
    _check_key(api_key)
    try:
        open_data = build_open_data(req.receiver, req.iban, req.code, req.purpose, req.amount)
        nbu_url, token = to_nbu_url(open_data)
        pay_url = f"{BASE_URL}/p/{token}"
        png_bytes = generate_qr_png_bytes(pay_url)
        qr_b64 = base64.b64encode(png_bytes).decode("ascii")
        return QRResponse(url=nbu_url, pay_url=pay_url, qr_base64=qr_b64)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qr.png")
def qr_png(receiver: str, iban: str, code: str, purpose: str, amount: str | None = None, api_key: str | None = None):
    _check_key(api_key)
    try:
        open_data = build_open_data(receiver, iban, code, purpose, amount)
        _, token = to_nbu_url(open_data)
        pay_url = f"{BASE_URL}/p/{token}"
        png_bytes = generate_qr_png_bytes(pay_url)
        return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/p/{token}", response_class=HTMLResponse)
def pay_page(token: str):
    nbu_url = f"https://bank.gov.ua/qr/{token}"
    html = f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VilnoPayService — Оплата</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f4f8; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; }}
  .card {{ background: white; border-radius: 20px; padding: 36px 28px; max-width: 420px; width: 100%; box-shadow: 0 4px 24px rgba(0,0,0,0.08); text-align: center; }}
  .logo {{ font-size: 22px; font-weight: 700; color: #1a1a2e; margin-bottom: 4px; }}
  .logo span {{ color: #2563eb; }}
  .subtitle {{ font-size: 13px; color: #888; margin-bottom: 28px; }}
  .label {{ font-size: 13px; color: #888; margin-bottom: 16px; }}
  .banks {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }}
  .bank-btn {{ display: flex; align-items: center; justify-content: center; gap: 8px; padding: 14px 10px; border-radius: 12px; border: 1.5px solid #e5e7eb; text-decoration: none; color: #1a1a2e; font-size: 14px; font-weight: 500; transition: all 0.15s; background: white; }}
  .bank-btn:hover {{ border-color: #2563eb; background: #eff6ff; }}
  .bank-btn img {{ width: 28px; height: 28px; object-fit: contain; }}
  .divider {{ border: none; border-top: 1px solid #f0f0f0; margin: 20px 0; }}
  .other-btn {{ display: block; width: 100%; padding: 14px; border-radius: 12px; background: #2563eb; color: white; font-size: 15px; font-weight: 600; text-decoration: none; border: none; cursor: pointer; }}
  .other-btn:hover {{ background: #1d4ed8; }}
  .footer {{ margin-top: 24px; font-size: 11px; color: #bbb; }}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Vilno<span>Pay</span></div>
  <div class="subtitle">Безпечна оплата через банківський застосунок</div>
  <div class="label">Оберіть ваш банк для оплати:</div>
  <div class="banks">
    <a class="bank-btn" href="{nbu_url}">
      <img src="https://www.privatbank.ua/favicon.ico" alt="">ПриватБанк
    </a>
    <a class="bank-btn" href="{nbu_url}">
      <img src="https://monobank.ua/favicon.ico" alt="">Monobank
    </a>
    <a class="bank-btn" href="{nbu_url}">
      <img src="https://sense.com.ua/favicon.ico" alt="">Sense Bank
    </a>
    <a class="bank-btn" href="{nbu_url}">
      <img src="https://pumb.ua/favicon.ico" alt="">ПУМБ
    </a>
  </div>
  <hr class="divider">
  <a class="other-btn" href="{nbu_url}">Інший банк</a>
  <div class="footer">VilnoPayService</div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)
