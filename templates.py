"""
VilnoPayService — HTML-шаблони.
"""
import html as _h


def _css_vars(s):
    bg = _h.escape(s.get("bg_color", "#f1f5f9"))
    pc = _h.escape(s.get("primary_color", "#2563eb"))
    ac = _h.escape(s.get("accent_color", "#16a34a"))
    cc = s.get("custom_css", "")
    return bg, pc, ac, cc

def _e(v):
    return _h.escape(str(v))


COPY_JS = """
<script>
function copyField(btn,fid){
  var t=document.getElementById(fid).textContent.trim();
  navigator.clipboard.writeText(t).then(function(){_ok(btn)}).catch(function(){_fb(t);_ok(btn)});
}
function copyAll(btn){
  var r=document.getElementById('v-receiver').textContent.trim();
  var i=document.getElementById('v-iban').textContent.trim();
  var p=document.getElementById('v-purpose').textContent.trim();
  var a=document.getElementById('v-amount').textContent.trim();
  var t='Отримувач: '+r+'\\nIBAN: '+i+'\\nПризначення: '+p+'\\nСума: '+a;
  navigator.clipboard.writeText(t).then(function(){_okAll(btn)}).catch(function(){_fb(t);_okAll(btn)});
}
function _ok(b){b.classList.add('ok');b.innerHTML='✅<span class="tip">Скопійовано!</span>';
  setTimeout(function(){b.classList.remove('ok');b.innerHTML='📋<span class="tip">Скопійовано!</span>';},2200);}
function _okAll(b){b.classList.add('ok');b.textContent='✅ Скопійовано!';
  setTimeout(function(){b.classList.remove('ok');b.textContent='📋 Скопіювати всі реквізити';},2500);}
function _fb(t){var a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';
  document.body.appendChild(a);a.select();document.execCommand('copy');document.body.removeChild(a);}

// Web Share API — оплатити через додаток банку
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
      await navigator.share({
        files:[file],
        title:'QR-код для оплати',
        text:'Відкрийте додаток банку та відскануйте цей QR-код'
      });
      trackAction('share_success');
    }else{
      // Fallback: завантажити зображення
      var url=URL.createObjectURL(blob);
      var a=document.createElement('a');
      a.href=url;a.download='qr-payment.png';a.click();
      URL.revokeObjectURL(url);
      trackAction('download_fallback');
      showToast('Збережіть зображення → відкрийте додаток банку → відскануйте QR з галереї');
    }
  }catch(e){
    if(e.name!=='AbortError'){
      trackAction('share_error');
      showToast('Збережіть зображення QR → відкрийте додаток банку → відскануйте QR з галереї');
    }
  }finally{
    btn.classList.remove('sharing');
  }
}

function showToast(m){
  var t=document.getElementById('share-toast');
  if(!t)return;
  t.textContent=m;t.classList.add('show');
  setTimeout(function(){t.classList.remove('show');},5000);
}

function trackAction(action){
  fetch('/track/bank-click',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({link_id:LINK_ID,bank:action})}).catch(function(){});
}
</script>
"""


def pay_page_html(nbu_url, receiver, iban, purpose, amount_line, qr_b64,
                  hours_left, settings=None, logo_url="", link_id=""):
    s = settings or {}
    bg, pc, ac, cc = _css_vars(s)
    pt = _e(s.get("page_title", "VilnoPay"))
    ps = _e(s.get("page_subtitle", "Безпечна оплата через банківський застосунок"))
    ft = _e(s.get("footer_text", "VilnoPayService · Захищено"))
    lu = _e(logo_url) if logo_url else _e(s.get("logo_url", ""))

    receiver = _e(receiver); iban = _e(iban)
    purpose = _e(purpose); amount_line = _e(amount_line)
    nbu = _e(nbu_url)

    logo = f'<img src="{lu}" alt="Logo" style="max-height:48px;margin-bottom:8px;border-radius:8px;">' if lu else ""

    def req_row(label, vid, value, mono=False):
        mc = ' mono' if mono else ''
        return f'''<div class="req-row"><div class="req-left"><div class="req-label">{label}</div>
<div class="req-value{mc}" id="{vid}">{value}</div></div>
<button class="copy-field" onclick="copyField(this,'{vid}')">📋<span class="tip">Скопійовано!</span></button></div>'''

    reqs = req_row("Отримувач", "v-receiver", receiver)
    reqs += req_row("IBAN", "v-iban", iban, mono=True)
    reqs += req_row("Призначення платежу", "v-purpose", purpose)
    reqs += req_row("Сума", "v-amount", amount_line)

    return f"""<!DOCTYPE html>
<html lang="uk"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<meta name="theme-color" content="{pc}">
<title>{pt} — Оплата</title>
<style>
:root{{--blue:{pc};--green:{ac};--bg:{bg};--card:#fff;--text:#0f172a;--text2:#334155;
--muted:#64748b;--border:#e2e8f0;--blue-lt:#eff6ff;--blue-bd:#bfdbfe;
--green-lt:#f0fdf4;--green-bd:#bbf7d0;--amber:#f59e0b;
--sh:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);--r:18px;--rs:12px;--t:.17s ease}}
@media(prefers-color-scheme:dark){{:root{{--bg:{bg};--card:#1e293b;--text:#f1f5f9;--text2:#cbd5e1;
--muted:#94a3b8;--border:#334155;--blue-lt:#172554;--blue-bd:#1e40af;
--green-lt:#052e16;--green-bd:#14532d;--sh:0 1px 3px rgba(0,0,0,.3),0 4px 12px rgba(0,0,0,.2)}}}}
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--text);min-height:100svh;padding:0 0 env(safe-area-inset-bottom,24px)}}
.wrap{{max-width:480px;margin:0 auto;padding:8px 14px 32px}}
.logo{{text-align:center;padding:18px 0 14px}}
.logo-mark{{font-size:24px;font-weight:800;letter-spacing:-.5px;color:var(--blue)}}
.logo-sub{{font-size:12px;color:var(--muted);margin-top:4px}}
.badge-ttl{{display:inline-flex;align-items:center;gap:5px;margin-top:10px;font-size:11px;color:var(--muted);background:var(--card);border:1px solid var(--border);border-radius:99px;padding:3px 10px 3px 8px}}
.dot{{width:6px;height:6px;border-radius:50%;background:var(--amber);animation:pulse-dot 2.2s ease-in-out infinite}}
@keyframes pulse-dot{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.4;transform:scale(.7)}}}}
.section{{background:var(--card);border-radius:var(--r);padding:16px 16px 18px;margin-bottom:10px;box-shadow:var(--sh);border:1px solid var(--border);animation:fade-up .35s ease both}}
@keyframes fade-up{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:none}}}}
.sec-head{{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}}
.amount-chip{{display:inline-flex;align-items:center;gap:6px;background:var(--blue-lt);border:1px solid var(--blue-bd);color:var(--blue);font-size:21px;font-weight:800;border-radius:var(--rs);padding:6px 14px;margin-bottom:14px}}

/* Кнопка «Оплатити через додаток» */
.share-btn{{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:16px;border-radius:var(--rs);border:none;background:var(--blue);color:#fff;font-size:16px;font-weight:700;cursor:pointer;transition:all var(--t);margin-bottom:12px}}
.share-btn:active{{transform:scale(.97)}}
.share-btn.sharing{{opacity:.6}}
.share-icon{{font-size:22px}}

/* Підказки */
.hint-box{{background:var(--blue-lt);border:1px solid var(--blue-bd);border-radius:var(--rs);padding:12px 14px;margin-bottom:0}}
.hint-box ol{{margin:0;padding-left:18px}}
.hint-box li{{font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:4px}}
.hint-box li:last-child{{margin-bottom:0}}
.hint-box li strong{{color:var(--blue)}}

/* QR */
.qr-wrap{{text-align:center;padding:4px 0 2px}}
.qr-img{{width:min(260px,85vw);height:min(260px,85vw);border-radius:14px;border:1px solid var(--border);display:block;margin:0 auto;background:#fff}}
.qr-hint{{font-size:12px;color:var(--muted);margin-top:10px}}

/* Реквізити */
.req-row{{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);gap:8px}}
.req-row:last-of-type{{border-bottom:none}}
.req-left{{flex:1;min-width:0}}
.req-label{{font-size:11px;color:var(--muted);font-weight:500;margin-bottom:2px}}
.req-value{{font-size:14px;font-weight:600;color:var(--text);word-break:break-all;line-height:1.35}}
.req-value.mono{{font-family:"SF Mono","Fira Code",monospace;font-size:13px}}
.copy-field{{flex-shrink:0;display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1.5px solid var(--border);background:var(--card);cursor:pointer;font-size:16px;transition:all var(--t);position:relative}}
.copy-field:active{{transform:scale(.86)}}
.copy-field.ok{{border-color:var(--green);background:var(--green-lt)}}
.copy-field .tip{{position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%) scale(.8);background:#1e293b;color:#fff;font-size:11px;font-weight:600;border-radius:6px;padding:3px 8px;white-space:nowrap;pointer-events:none;opacity:0;transition:all var(--t)}}
.copy-field.ok .tip{{opacity:1;transform:translateX(-50%) scale(1)}}
.copy-all{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;margin-top:14px;padding:13px;border-radius:var(--rs);border:1.5px solid var(--border);background:var(--card);font-size:14px;font-weight:700;color:var(--blue);cursor:pointer;transition:all var(--t)}}
.copy-all:active{{transform:scale(.97)}}
.copy-all.ok{{color:var(--green);border-color:var(--green);background:var(--green-lt)}}
.footer{{text-align:center;font-size:10px;color:var(--muted);opacity:.4;padding:14px 0 4px}}

/* Toast */
.share-toast{{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(20px);background:#1e293b;color:#fff;padding:14px 20px;border-radius:12px;font-size:13px;font-weight:600;max-width:90vw;text-align:center;opacity:0;transition:all .3s;z-index:999;box-shadow:0 4px 20px rgba(0,0,0,.3)}}
.share-toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
{cc}
</style></head>
<body style="background:{bg};"><div class="wrap">
<div class="logo">{logo}<div class="logo-mark">{pt}</div><div class="logo-sub">{ps}</div>
<div class="badge-ttl"><span class="dot"></span>Посилання активне ще {hours_left} год.</div></div>

<div class="section">
<div class="sec-head">Оплата через додаток</div>
<div class="amount-chip">💳 {amount_line}</div>
<button class="share-btn" id="share-btn" onclick="shareQR()">
<span class="share-icon">🏦</span> Оплатити через додаток банку
</button>
<div class="hint-box">
<ol>
<li><strong>Натисніть кнопку вище</strong> «Оплатити через додаток банку»</li>
<li><strong>Оберіть додаток вашого банку</strong> у вікні, що з'явиться</li>
<li>Додаток банку <strong>автоматично розпізнає QR</strong> і заповнить усі реквізити</li>
<li>Перевірте суму та <strong>підтвердьте платіж</strong></li>
</ol>
</div>
</div>

<div class="section">
<div class="sec-head">Сканувати QR-код</div>
<div class="qr-wrap">
<img class="qr-img" id="qr-image" src="data:image/png;base64,{qr_b64}" alt="QR код" loading="eager">
<div class="qr-hint">Відскануйте камерою додатку вашого банку</div>
</div>
</div>

<div class="section">
<div class="sec-head">Реквізити для оплати</div>
{reqs}
<button class="copy-all" onclick="copyAll(this)">📋 Скопіювати всі реквізити</button>
</div>

<div class="footer">{ft}</div>
</div>
<div class="share-toast" id="share-toast"></div>
<script>var LINK_ID="{link_id}";</script>
{COPY_JS}</body></html>"""


def expired_page_html():
    return """<!DOCTYPE html>
<html lang="uk"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>VilnoPay — Посилання застаріло</title>
<style>body{font-family:-apple-system,sans-serif;background:#f1f5f9;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:white;border-radius:20px;padding:40px 32px;max-width:360px;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,.08)}
.icon{font-size:48px;margin-bottom:16px}h2{font-size:20px;color:#1e293b;margin-bottom:8px}
p{font-size:14px;color:#64748b;line-height:1.5}.footer{margin-top:24px;font-size:11px;color:#cbd5e1}</style></head>
<body><div class="card"><div class="icon">⏰</div>
<h2>Посилання застаріло</h2>
<p>Термін дії цього платіжного посилання закінчився. Будь ласка, зверніться до продавця для отримання нового посилання.</p>
<div class="footer">VilnoPayService</div></div></body></html>"""