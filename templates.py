"""
VilnoPayService — HTML-шаблони.
"""
import html as _h


def _css_vars(s):
    bg = _h.escape(s.get("bg_color", "#F8F9FB"))
    pc = _h.escape(s.get("primary_color", "#1D6F42"))
    ac = _h.escape(s.get("accent_color", "#1D6F42"))
    tc = _h.escape(s.get("text_color", "#101828"))
    cc_color = _h.escape(s.get("card_color", "#FFFFFF"))
    bc = _h.escape(s.get("border_color", "#EAECF0"))
    ff = _h.escape(s.get("font_family", "Inter"))
    fs = _h.escape(s.get("font_size", "15"))
    cc = s.get("custom_css", "")
    return bg, pc, ac, tc, cc_color, bc, ff, fs, cc

def _e(v):
    return _h.escape(str(v))


COPY_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>'

BANK_ICON = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M3 10h18"/><path d="M5 6l7-3 7 3"/><path d="M4 10v11"/><path d="M20 10v11"/><path d="M8 10v11"/><path d="M16 10v11"/></svg>'

COPY_JS = """
<script>
function copyField(btn,fid){
  var t=document.getElementById(fid).textContent.trim();
  navigator.clipboard.writeText(t).then(function(){_ok(btn)}).catch(function(){_fb(t);_ok(btn)});
}
function copyAll(btn){
  var r=document.getElementById('v-receiver').textContent.trim();
  var i=document.getElementById('v-iban').textContent.trim();
  var c=document.getElementById('v-code').textContent.trim();
  var p=document.getElementById('v-purpose').textContent.trim();
  var a=document.getElementById('v-amount').textContent.trim();
  var t='Отримувач: '+r+'\\nIBAN: '+i+'\\nІПН: '+c+'\\nПризначення: '+p+'\\nСума: '+a;
  navigator.clipboard.writeText(t).then(function(){_okAll(btn)}).catch(function(){_fb(t);_okAll(btn)});
}
function _ok(b){b.classList.add('ok');showToast('Скопійовано');}
function _okAll(b){b.classList.add('ok');b.textContent='✓ Скопійовано';setTimeout(function(){b.classList.remove('ok');b.textContent='Скопіювати всі реквізити';},2500);}
function _fb(t){var a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();document.execCommand('copy');document.body.removeChild(a);}

async function shareQR(){
  var btn=document.getElementById('share-btn');
  try{
    btn.classList.add('sharing');
    var img=document.getElementById('qr-image');
    var b64=img.src.split(',')[1];
    var bin=atob(b64);
    var arr=new Uint8Array(bin.length);
    for(var i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);
    var blob=new Blob([arr],{type:'image/png'});
    var file=new File([blob],'qr-payment.png',{type:'image/png'});
    if(navigator.canShare&&navigator.canShare({files:[file]})){
      await navigator.share({files:[file],title:'QR-код для оплати',text:'Відкрийте додаток банку та відскануйте цей QR-код'});
      trackAction('share_success');
    }else{
      var url=URL.createObjectURL(blob);
      var a=document.createElement('a');a.href=url;a.download='qr-payment.png';a.click();
      URL.revokeObjectURL(url);
      trackAction('download_fallback');
      showToast('Збережіть зображення → відкрийте додаток банку → відскануйте QR з галереї');
    }
  }catch(e){
    if(e.name!=='AbortError'){trackAction('share_error');showToast('Затисніть QR-зображення → «Поділитися» → оберіть додаток банку');}
  }finally{btn.classList.remove('sharing');}
}

function showToast(m){var t=document.getElementById('toast');if(!t)return;t.textContent=m;t.classList.add('show');setTimeout(function(){t.classList.remove('show');},3000);}
function trackAction(action){fetch('/track/bank-click',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({link_id:LINK_ID,bank:action})}).catch(function(){});}
</script>
"""


def pay_page_html(nbu_url, receiver, iban, purpose, amount_line, qr_b64,
                  hours_left, settings=None, logo_url="", link_id="", code=""):
    s = settings or {}
    bg, pc, ac, tc, cc_color, bc, ff, fs, cc = _css_vars(s)
    pt = _e(s.get("page_title", "VilnoPay"))
    ps = _e(s.get("page_subtitle", "Безпечна оплата переказом"))
    ft = _e(s.get("footer_text", "VilnoPayService · Захищено"))
    lu = _e(logo_url) if logo_url else _e(s.get("logo_url", ""))

    receiver = _e(receiver); iban = _e(iban)
    purpose = _e(purpose); amount_line = _e(amount_line)
    code_e = _e(code)

    # Форматування суми
    amt_display = amount_line

    logo = f'<img src="{lu}" alt="Logo" style="max-height:44px;margin-bottom:8px;border-radius:8px;">' if lu else ""

    def req_row(label, vid, value, mono=False):
        mc = ' mono' if mono else ''
        return f'''<div class="req-row"><div class="req-left"><div class="req-label">{label}</div>
<div class="req-value{mc}" id="{vid}">{value}</div></div>
<button class="copy-field" onclick="copyField(this,'{vid}')">{COPY_ICON}</button></div>'''

    reqs = req_row("Отримувач", "v-receiver", receiver)
    reqs += req_row("IBAN", "v-iban", iban, mono=True)
    reqs += req_row("ІПН (РНКОПП)", "v-code", code_e, mono=True)
    reqs += req_row("Призначення платежу", "v-purpose", purpose)
    reqs += req_row("Сума", "v-amount", amt_display)

    return f"""<!DOCTYPE html>
<html lang="uk"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<meta name="theme-color" content="{pc}">
<title>{pt} — Оплата</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{{--primary:{pc};--accent:{ac};--bg:{bg};--card:{cc_color};--text:{tc};--muted:#667085;
--border:{bc};--primary-lt:#F0FAF4;--primary-bd:#A9D6B8;
--danger:#D92D20;
--sh:0 1px 2px rgba(16,24,40,.04),0 4px 16px rgba(16,24,40,.06);
--r:16px;--rs:10px;--t:.17s ease}}
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:{fs}px;background:var(--bg);color:var(--text);min-height:100svh;padding:0 0 env(safe-area-inset-bottom,24px);-webkit-font-smoothing:antialiased}}
.wrap{{max-width:440px;margin:0 auto;padding:8px 16px 32px}}

/* Header */
.header{{text-align:center;padding:20px 0 16px}}
.header img{{max-height:44px;border-radius:8px;margin-bottom:8px}}
.header h1{{font-size:20px;font-weight:700;letter-spacing:-.02em;color:var(--text)}}
.header p{{font-size:13px;color:var(--muted);margin-top:3px}}
.badge-ttl{{display:inline-flex;align-items:center;gap:5px;margin-top:10px;font-size:11px;color:var(--muted);background:var(--card);border:1px solid var(--border);border-radius:99px;padding:3px 10px 3px 8px}}
.dot{{width:6px;height:6px;border-radius:50%;background:#F59E0B;animation:pulse-dot 2.2s ease-in-out infinite}}
@keyframes pulse-dot{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.4;transform:scale(.7)}}}}

/* Amount block */
.amount-block{{text-align:center;padding:20px 16px;background:var(--card);border-radius:var(--r);border:1px solid var(--border);box-shadow:var(--sh);margin-bottom:10px}}
.amount-label{{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}}
.amount-value{{font-size:32px;font-weight:800;color:var(--primary);letter-spacing:-1px;line-height:1}}
.amount-purpose{{font-size:13px;color:var(--muted);margin-top:6px}}

/* Card section */
.section{{background:var(--card);border-radius:var(--r);padding:16px;margin-bottom:10px;box-shadow:var(--sh);border:1px solid var(--border);animation:fade-up .3s ease both}}
@keyframes fade-up{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
.sec-label{{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}}

/* Pay button */
.pay-btn{{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:14px 20px;border-radius:var(--rs);border:none;background:var(--primary);color:#fff;font-size:15px;font-weight:600;letter-spacing:-.01em;cursor:pointer;transition:all var(--t);font-family:inherit}}
.pay-btn:active{{transform:scale(.97)}}
.pay-btn.sharing{{opacity:.6}}
.pay-btn svg{{flex-shrink:0}}

/* Steps */
.hint-steps{{display:flex;flex-direction:column;gap:8px;padding:12px 0 0}}
.hint-step{{display:flex;gap:10px;align-items:flex-start;font-size:13px;color:var(--muted);line-height:1.5}}
.hint-step-num{{flex-shrink:0;width:20px;height:20px;border-radius:50%;background:var(--primary-lt);color:var(--primary);font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center}}

/* Divider */
.divider{{display:flex;align-items:center;gap:12px;margin:6px 0;color:var(--muted);font-size:12px;font-weight:500}}
.divider::before,.divider::after{{content:'';flex:1;height:1px;background:var(--border)}}

/* QR */
.qr-wrap{{text-align:center;padding:4px 0 2px}}
.qr-img{{width:min(240px,80vw);height:min(240px,80vw);border-radius:12px;border:1px solid var(--border);display:block;margin:0 auto;background:#fff;cursor:pointer;transition:transform var(--t)}}
.qr-img:active{{transform:scale(.95)}}
.qr-tap{{font-size:12px;color:var(--primary);font-weight:500;margin-top:8px}}

/* Requisites */
.req-row{{display:flex;justify-content:space-between;align-items:center;padding:11px 0;border-bottom:1px solid var(--border);gap:8px}}
.req-row:last-of-type{{border-bottom:none}}
.req-left{{flex:1;min-width:0}}
.req-label{{font-size:11px;color:var(--muted);font-weight:500;margin-bottom:2px}}
.req-value{{font-size:15px;font-weight:600;color:var(--text);word-break:break-all;line-height:1.35}}
.req-value.mono{{font-family:'SF Mono','Fira Code',monospace;font-size:13px}}
.copy-field{{flex-shrink:0;display:flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all var(--t)}}
.copy-field:active{{transform:scale(.86)}}
.copy-field.ok{{color:var(--primary);border-color:var(--primary);background:var(--primary-lt)}}
.copy-all{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;margin-top:14px;padding:12px;border-radius:var(--rs);border:1px solid var(--border);background:transparent;font-size:14px;font-weight:600;color:var(--text);cursor:pointer;transition:all var(--t);font-family:inherit}}
.copy-all:active{{transform:scale(.97)}}
.copy-all.ok{{color:var(--primary);border-color:var(--primary);background:var(--primary-lt)}}
.footer{{text-align:center;font-size:11px;color:var(--muted);padding:16px 0 8px}}

/* Toast */
#toast{{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--text);color:var(--card);padding:12px 20px;border-radius:10px;font-size:13px;font-weight:600;max-width:90vw;text-align:center;opacity:0;transition:all .3s;z-index:999;box-shadow:0 4px 20px rgba(0,0,0,.2)}}
#toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
{cc}
</style></head>
<body><div class="wrap">

<div class="header">
{logo}
<h1>{pt}</h1>
<p>{ps}</p>
<div class="badge-ttl"><span class="dot"></span>Активне ще {hours_left} год.</div>
</div>

<div class="amount-block">
<div class="amount-label">Сума до сплати</div>
<div class="amount-value">{amt_display}</div>
<div class="amount-purpose">{purpose}</div>
</div>

<div class="section">
<button class="pay-btn" id="share-btn" onclick="shareQR()">
{BANK_ICON} Оплатити через банк
</button>
<div class="hint-steps">
<div class="hint-step"><span class="hint-step-num">1</span><span>Натисніть кнопку вище «Оплатити через банк»</span></div>
<div class="hint-step"><span class="hint-step-num">2</span><span>Оберіть додаток вашого банку у вікні</span></div>
<div class="hint-step"><span class="hint-step-num">3</span><span>Підтвердьте платіж у додатку банку</span></div>
</div>
</div>

<div class="divider"><span>або відскануйте QR-код</span></div>

<div class="section">
<div class="qr-wrap">
<img class="qr-img" id="qr-image" src="data:image/png;base64,{qr_b64}" alt="QR код" loading="eager" onclick="shareQR()">
<div class="qr-tap">Натисніть на QR-код, щоб поділитися з додатком банку</div>
</div>
</div>

<div class="section">
<div class="sec-label">Реквізити для переказу</div>
{reqs}
<button class="copy-all" onclick="copyAll(this)">Скопіювати всі реквізити</button>
</div>

<div class="footer">{ft}</div>
</div>
<div id="toast"></div>
<script>var LINK_ID="{link_id}";</script>
{COPY_JS}</body></html>"""


def expired_page_html():
    return """<!DOCTYPE html>
<html lang="uk"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>VilnoPay — Посилання застаріло</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>body{font-family:'Inter',-apple-system,sans-serif;background:#F8F9FB;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:16px;padding:40px 32px;max-width:360px;text-align:center;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 16px rgba(16,24,40,.06);border:1px solid #EAECF0}
.icon{font-size:48px;margin-bottom:16px}h2{font-size:20px;color:#101828;margin-bottom:8px;font-weight:700}
p{font-size:14px;color:#667085;line-height:1.5}.footer{margin-top:24px;font-size:11px;color:#667085}</style></head>
<body><div class="card"><div class="icon">⏰</div>
<h2>Посилання застаріло</h2>
<p>Термін дії цього платіжного посилання закінчився. Зверніться до продавця для нового посилання.</p>
<div class="footer">VilnoPayService</div></div></body></html>"""