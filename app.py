import base64
import io
import os
import qrcode
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="NBU QR Generator", version="1.0.0")
API_KEY = os.getenv("API_KEY", "")

class QRRequest(BaseModel):
    receiver: str
    iban: str
    code: str
    purpose: str
    amount: str | None = None

class QRResponse(BaseModel):
    url: str
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
    return f"https://bank.gov.ua/qr/{token}"

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
    return {"status": "ok"}

@app.post("/generate", response_model=QRResponse)
def generate(req: QRRequest, api_key: str | None = None):
    _check_key(api_key)
    try:
        open_data = build_open_data(req.receiver, req.iban, req.code, req.purpose, req.amount)
        url = to_nbu_url(open_data)
        png_bytes = generate_qr_png_bytes(url)
        qr_b64 = base64.b64encode(png_bytes).decode("ascii")
        return QRResponse(url=url, qr_base64=qr_b64)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/qr.png")
def qr_png(receiver: str, iban: str, code: str, purpose: str, amount: str | None = None, api_key: str | None = None):
    _check_key(api_key)
    try:
        open_data = build_open_data(receiver, iban, code, purpose, amount)
        url = to_nbu_url(open_data)
        png_bytes = generate_qr_png_bytes(url)
        return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
