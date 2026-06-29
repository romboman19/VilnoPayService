# UI/UX Review — VilnoPayService

> Аналіз проведено з позиції UI/UX designer. Кожна рекомендація містить: файл і рядок, поточний стан, конкретний CSS/HTML код для виправлення, пріоритет (High/Medium/Low).

---

## Зміст

1. [Сторінка оплати (templates.py)](#1-сторінка-оплати)
2. [Адмінка (admin.html)](#2-адмінка)
3. [Кабінет менеджера (manager.html)](#3-кабінет-менеджера)
4. [Консистентність](#4-консистентність)
5. [Accessibility](#5-accessibility)
6. [Резюме пріоритетів](#6-резюме-пріоритетів)

---

## 1. Сторінка оплати

### UX-01 · Картки реквізитів злипаються — відсутній `.req-container` wrapper
**Файл:** `templates.py`, функція `pay_page_html` — рядки де формується `requisites_block` (~рядок 163)  
**Пріоритет:** 🔴 High

**Поточний стан:**
```python
requisites_block = f"""<div class="section">
<div class="sec-label">Реквізити для переказу</div>
{reqs}
<button class="copy-all" ...>...</button>
</div>"""
```
CSS клас `.req-container` оголошений (`display:flex; flex-direction:column; gap:14px`), але `{reqs}` вставляється напряму в `.section` без обгортки. Картки злипаються без gap.

**Виправлення:**
```python
requisites_block = f"""<div class="section">
<div class="sec-label">Реквізити для переказу</div>
<div class="req-container">
{reqs}
</div>
<button class="copy-all" onclick="copyAll(this)">Скопіювати всі реквізити</button>
</div>"""
```

---

### UX-02 · Toast перекривається home bar на iOS
**Файл:** `templates.py`, CSS `#toast` (~рядок ~110 у CSS-рядку)  
**Пріоритет:** 🔴 High

**Поточний стан:**
```css
#toast { position:fixed; bottom:20px; left:50%; ... }
```
На iPhone з home indicator (≥ iPhone X) системний рядок перекриває toast.

**Виправлення:**
```css
#toast {
  position: fixed;
  bottom: max(20px, calc(env(safe-area-inset-bottom, 0px) + 12px));
  left: 50%;
  transform: translateX(-50%) translateY(20px);
  background: var(--text);
  color: var(--card);
  padding: 12px 20px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 600;
  max-width: 90vw;
  text-align: center;
  opacity: 0;
  transition: all .3s;
  z-index: 999;
  box-shadow: 0 4px 20px rgba(0,0,0,.2);
}
```

---

### UX-03 · Розмір суми не масштабується — зламується при великих значеннях
**Файл:** `templates.py`, CSS `.amount-value`  
**Пріоритет:** 🔴 High

**Поточний стан:**
```css
.amount-value { font-size:32px; font-weight:800; }
```
При значеннях типу «123 456.78 грн» або на екрані 360px рядок обрізається або переноситься некерованого.

**Виправлення:**
```css
.amount-value {
  font-size: clamp(20px, 7.5vw, 36px);
  font-weight: 800;
  color: var(--primary);
  letter-spacing: -0.5px;
  line-height: 1.1;
  word-break: break-all;
}
```

---

### UX-04 · LiqPay widget container обрізає контент при розвантаженні
**Файл:** `templates.py`, функція `liqpay_block_html`, рядок з `liqpay-widget-container`  
**Пріоритет:** 🔴 High

**Поточний стан:**
```html
<div id="liqpay-widget-container" 
     style="min-height:200px;display:flex;align-items:center;justify-content:center">
```
LiqPay checkout widget після рендеру займає ~320–400px. Container фіксований — нижня частина форми обрізається.

**Виправлення:**
```html
<div id="liqpay-widget-container" 
     style="min-height:200px; width:100%; overflow:visible;">
  <div style="display:flex;align-items:center;justify-content:center;min-height:200px;color:var(--muted);font-size:13px;">
    Завантаження...
  </div>
</div>
```
І додати в JS після `LiqPayCheckout.init(...)`:
```javascript
.on("liqpay.ready", function() {
  var c = document.getElementById('liqpay-widget-container');
  c.style.minHeight = 'auto';
  c.style.display = 'block';
});
```

---

### UX-05 · Дублювання призначення платежу
**Файл:** `templates.py`, CSS `.amount-purpose` + блок реквізитів  
**Пріоритет:** 🟡 Medium

**Поточний стан:** Призначення показується двічі — в `.amount-purpose` (під сумою) і в картці «Призначення платежу» у реквізитах.

**Виправлення — варіант A** (скоротити в amount-block):
```css
.amount-purpose {
  font-size: 12px;
  color: var(--muted);
  margin-top: 5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 280px;
  margin-left: auto;
  margin-right: auto;
}
```
**Варіант B** (видалити з amount-block):
```python
# У pay_page_html прибрати рядок:
# <div class="amount-purpose">{purpose}</div>
```

---

### UX-06 · Hover-стан кнопки «Оплатити» відсутній на desktop
**Файл:** `templates.py`, CSS `.pay-btn`  
**Пріоритет:** 🟡 Medium

**Поточний стан:**
```css
.pay-btn:active { transform:scale(.97) }
```
На desktop при наведенні кнопка не реагує — виглядає неінтерактивно.

**Виправлення:**
```css
.pay-btn:hover:not(:active) {
  filter: brightness(1.1);
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(29, 111, 66, 0.30);
}
.pay-btn:active {
  transform: scale(0.97) translateY(0);
  filter: none;
  box-shadow: none;
}
```

---

### UX-07 · Invoice block — жорсткі кольори не адаптуються до брендингу
**Файл:** `templates.py`, CSS `.invoice-block`, `.invoice-link`  
**Пріоритет:** 🟡 Medium

**Поточний стан:**
```css
.invoice-block { background:#f0f9ff; border:1px solid #bae6fd; }
.invoice-link { color:#0284c7; }
```
Кольори захардкоджені — не реагують на `--primary` та `--primary-lt` CSS-змінні. При зеленому/чорному брендингу блок виглядає чужорідно.

**Виправлення:**
```css
.invoice-block {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--primary-lt);
  border: 1px solid var(--primary-bd);
  border-radius: var(--rs);
  padding: 14px 18px;
  margin: 10px 0;
}
.invoice-icon { font-size: 24px; flex-shrink: 0; }
.invoice-text { display: flex; flex-direction: column; gap: 4px; }
.invoice-text span { font-size: 12px; color: var(--muted); }
.invoice-link {
  font-size: 14px;
  font-weight: 600;
  color: var(--primary);
  text-decoration: none;
}
.invoice-link:hover { text-decoration: underline; }
```

---

### UX-08 · QR skeleton під час завантаження base64
**Файл:** `templates.py`, CSS `.qr-img`  
**Пріоритет:** 🟡 Medium

**Поточний стан:**
```css
.qr-img { background:#fff; }
```
При повільному з'єднанні або великому base64 — порожній квадрат без індикатора.

**Виправлення:**
```css
.qr-img {
  width: min(240px, 80vw);
  height: min(240px, 80vw);
  border-radius: 12px;
  border: 1px solid var(--border);
  display: block;
  margin: 0 auto;
  cursor: pointer;
  transition: transform var(--t), opacity 0.3s;
  background: linear-gradient(90deg, #f0f0f0 25%, #e8e8e8 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: img-shimmer 1.5s infinite;
}
.qr-img[src]:not([src=""]) {
  animation: none;
  background: #ffffff;
}
@keyframes img-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
```

---

### UX-09 · TTL Badge — dot не сигналізує критичний час
**Файл:** `templates.py`, JS функція `updateTTL()`  
**Пріоритет:** 🟢 Low

**Поточний стан:** Точка завжди жовта. Користувач не бачить, що залишилося, наприклад, 3 хвилини.

**Виправлення — додати до `updateTTL()`:**
```javascript
function updateTTL() {
  var el = document.getElementById('ttl-text');
  if (!el) return;
  var dot = document.querySelector('.dot');
  if (TTL_SEC <= 0) {
    el.textContent = 'Посилання неактивне';
    if (dot) dot.style.background = '#D92D20';
    return;
  }
  var h = Math.floor(TTL_SEC / 3600);
  var m = Math.floor((TTL_SEC % 3600) / 60);
  el.textContent = 'Посилання активне ще ' + h + ' год ' + m + ' хв';
  // Колір dot залежно від часу
  if (dot) {
    if (TTL_SEC < 600)       dot.style.background = '#D92D20'; // < 10 хв — червоний
    else if (TTL_SEC < 3600) dot.style.background = '#F59E0B'; // < 1 год — жовтий
    else                     dot.style.background = '#12B76A'; // активний — зелений
  }
  TTL_SEC--;
}
```

---

### UX-10 · Hint-кроки під QR неактуальні для iOS
**Файл:** `templates.py`, `nbu_qr_block`  
**Пріоритет:** 🟢 Low

**Поточний стан:** Три кроки «Для оплати через додаток банку (Android)» завжди видимі. На iOS вони введуть в оману.

**Виправлення — додати в COPY_JS:**
```javascript
// Детектування платформи
(function() {
  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  var tapEl = document.querySelector('.qr-tap');
  var stepsEl = document.querySelector('.hint-steps');
  if (tapEl) {
    tapEl.textContent = isIOS
      ? 'Натисніть на QR-код, щоб поділитися з додатком банку (iOS)'
      : 'Для оплати через додаток банку (Android):';
  }
  if (stepsEl && isIOS) {
    stepsEl.innerHTML = '<div class="hint-step"><span class="hint-step-num">1</span>'
      + '<span>Натисніть на QR-код</span></div>'
      + '<div class="hint-step"><span class="hint-step-num">2</span>'
      + '<span>Оберіть «Поділитися» → ваш банківський додаток</span></div>';
  }
})();
```

---

### UX-11 · Кнопка copy-all — `height:52px` обрізає текст при wrap
**Файл:** `templates.py`, CSS `.copy-all`  
**Пріоритет:** 🟢 Low

**Поточний стан:** `height: 52px` — при вузькому екрані текст може обрізатись.

**Виправлення:**
```css
.copy-all {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  margin-top: 16px;
  min-height: 52px;
  height: auto;
  padding: 14px 20px;
  border-radius: var(--rs);
  border: 1px solid var(--border);
  background: transparent;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  transition: all var(--t);
  font-family: inherit;
  line-height: 1.3;
}
```

---

### UX-12 · Футер без візуального роздільника
**Файл:** `templates.py`, CSS `.footer`  
**Пріоритет:** 🟢 Low

**Поточний стан:** Футер іде прямо після карточок без роздільника — межа незрозуміла.

****Виправлення:**
```css
.footer {
  text-align: center;
  font-size: 11px;
  color: var(--muted);
  padding: 20px 0 12px;
  border-top: 1px solid var(--border);
  margin-top: 8px;
}
```

---

## 2. Адмінка

### ADM-01 · Focus state відсутній на табах
**Файл:** `admin.html`, CSS `.tab`
**Пріоритет:** 🔴 High

**Поточний стан:** Немає `:focus-visible` — при Tab/Shift+Tab таб не підсвічується.

**Виправлення:**
```css
.tab:focus-visible, .btn:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}
.tab:focus:not(:focus-visible), .btn:focus:not(:focus-visible) {
  outline: none;
}
```

---

### ADM-02 · Провайдер-поля LiqPay без візуального grouping
**Файл:** `admin.html`, `<div id="provider-fields">`
**Пріоритет:** 🟡 Medium

**Поточний стан:** Поля LiqPay ідуть без відокремлення від решти форми.

**Виправлення:**
```html
<div id="provider-fields" style="display:none">
  <div style="background:#F9FAFB;border:1px solid var(--border);border-radius:10px;
              padding:16px;margin-top:4px;display:flex;flex-direction:column;gap:12px;">
    <div style="font-size:12px;font-weight:700;color:var(--muted);
                text-transform:uppercase;letter-spacing:.06em;">Налаштування LiqPay</div>
    <div class="fg" style="margin-bottom:0">
      <label>LiqPay Public Key</label>
      <input id="rcv-lp-pub" placeholder="i00000000000" class="mono">
    </div>
    <div class="fg" style="margin-bottom:0">
      <label>LiqPay Private Key</label>
      <input id="rcv-lp-priv" type="password" class="mono">
      <div style="font-size:11px;color:var(--muted);margin-top:4px">
        Зашифровано. При редагуванні залиште порожнім щоб не змінювати.
      </div>
    </div>
    <div class="fg" style="margin-bottom:0">
      <label>Режим відображення</label>
      <select id="rcv-lp-mode">
        <option value="widget">Віджет (вбудований на сторінку)</option>
        <option value="redirect">Кнопка "Онлайн оплата" (redirect)</option>
      </select>
    </div>
    <label style="font-size:14px;cursor:pointer;display:flex;align-items:center;gap:8px;">
      <input type="checkbox" id="rcv-lp-sandbox"> Sandbox (тестовий режим)
    </label>
  </div>
</div>
```

---

### ADM-03 · Color picker — зайвий preview-span
**Файл:** `admin.html`, CSS `.color-field`
**Пріоритет:** 🟡 Medium

**Поточний стан:** 3 елементи (color + preview span + hex) — span дублює нативний picker.

**Виправлення:**
```html
<div class="color-field">
  <input type="text" class="color-hex" id="hex-bg_color" value="#F8F9FB" maxlength="7">
  <input type="color" id="s-bg_color" value="#F8F9FB"
         style="width:36px;height:36px;border-radius:8px;border:1px solid var(--border);
                padding:2px;cursor:pointer;flex-shrink:0;">
</div>
```
```css
/* Видалити .color-preview */
.color-hex { flex:1; }
```

---

### ADM-04 · Block order editor — кнопки стрілок замалі (tappable area менше WCAG 44px)
**Файл:** `admin.html`, `renderBlockOrder()`
**Пріоритет:** 🟡 Medium

**Поточний стан:** `<span onclick="...">↑</span>` — без фіксованого розміру.

**Виправлення:**
```javascript
function renderBlockOrder(order) {
  const labels = {
    nbu_qr: "🏦 QR НБУ + кнопка банку",
    liqpay: "💳 LiqPay",
    requisites: "📋 Реквізити"
  };
  const el = E("block-order-editor");
  el.innerHTML = "";
  order.forEach((b, i) => {
    const d = document.createElement("div");
    d.style.cssText = "display:flex;align-items:center;gap:8px;padding:10px 12px;" +
      "border:1px solid var(--border);border-radius:8px;background:var(--card);";
    d.innerHTML =
      `<span style="color:var(--muted);min-width:18px">${i+1}.</span>` +
      `<span style="flex:1;font-size:14px;font-weight:500">${labels[b]||b}</span>` +
      `<button onclick="moveBlock('${b}',-1)" aria-label="Вгору"
               style="min-width:36px;height:36px;border:1px solid var(--border);
                      border-radius:6px;background:var(--card);cursor:pointer;font-size:16px;">↑</button>` +
      `<button onclick="moveBlock('${b}',1)" aria-label="Вниз"
               style="min-width:36px;height:36px;border:1px solid var(--border);
                      border-radius:6px;background:var(--card);cursor:pointer;font-size:16px;">↓</button>`;
    el.appendChild(d);
  });
}
```

---

### ADM-05 · Таблиці без sticky header
**Файл:** `admin.html`, CSS `th`, `.resp-table`
**Пріоритет:** 🟡 Medium

**Виправлення:**
```css
.resp-table {
  overflow-x: auto;
  max-height: 500px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--r);
}
th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--card);
  box-shadow: 0 1px 0 var(--border);
}
```

---

### ADM-06 · Форма менеджерів — inline стилі, немає label
**Файл:** `admin.html`, `<div class="panel" id="p-mgrs">`
**Пріоритет:** 🟡 Medium

**Виправлення:**
```html
<div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border)">
  <h3 style="font-size:14px;font-weight:700;margin-bottom:12px">Новий менеджер</h3>
  <div class="fg">
    <label for="mgr-user">Логін</label>
    <input id="mgr-user" placeholder="manager01" autocomplete="off">
  </div>
  <div class="fg">
    <label for="mgr-pass">Пароль</label>
    <input id="mgr-pass" type="password" autocomplete="new-password">
  </div>
  <button class="btn btn-blue" onclick="createManager()"
          style="width:100%;margin-top:4px">+ Створити менеджера</button>
</div>
```

---

### ADM-07 · Кнопки видалення без aria-label
**Файл:** `admin.html`, `loadRcv()`, `loadManagers()`
**Пріоритет:** 🟡 Medium

**Виправлення:**
```javascript
// loadRcv():
`<button class="btn btn-red btn-sm"
   onclick="delRcv('${esc(r.receiver_key)}')"
   aria-label="Видалити ${esc(r.name)}">🗑</button>`

// loadManagers():
`<button class="btn btn-red btn-sm"
   onclick="delManager(${r.id})"
   aria-label="Видалити ${esc(r.username)}">🗑</button>`
```

---

### ADM-08 · Preview відкриває сторінку без збережених змін
**Файл:** `admin.html`, `previewBrand()`
**Пріоритет:** 🟢 Low

**Виправлення:**
```javascript
function previewBrand() {
  if (confirm("Зберегти зміни перед переглядом?")) {
    saveBrand().then(() => window.open("/admin/preview", "_blank"));
  } else {
    window.open("/admin/preview", "_blank");
  }
}
```

---

## 3. Кабінет менеджера

### MGR-01 · Кнопки «Створити» та «Шаблон» рівнозначні по вазі
**Файл:** `manager.html`, `<div class="panel active" id="p-pay">`
**Пріоритет:** 🟡 Medium

**Поточний стан:**
```html
<div class="flex mt">
  <button class="btn btn-green">Створити платіж</button>
  <button class="btn btn-outline">Завантажити шаблон</button>
</div>
```
На мобільному обидві кнопки в ряд — дуже вузькі. Немає ієрархії.

**Виправлення:**
```html
<div style="margin-top:16px;display:flex;flex-direction:column;gap:8px;">
  <button class="btn btn-green btn-block" onclick="createPayment()"
          style="font-size:16px;min-height:52px;">✓ Створити платіж</button>
  <button class="btn btn-outline btn-block" onclick="loadTemplatesForSelect()"
          style="font-size:13px;color:var(--muted);">Або використати шаблон →</button>
</div>
```

---

### MGR-02 · Result URL — не зрозуміло, що можна виділити
**Файл:** `manager.html`, CSS `.result-url`
**Пріоритет:** 🟡 Medium

**Виправлення:**
```css
.result-url {
  font-family: "SF Mono", monospace;
  font-size: 13px;
  word-break: break-all;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  cursor: text;
  user-select: all;
  -webkit-user-select: all;
  color: var(--blue);
}
.result-url:hover { border-color: var(--blue); }
```

---

### MGR-03 · copyHistoryLink — немає `.ok` класу, немає fallback
**Файл:** `manager.html`, `copyHistoryLink()`
**Пріоритет:** 🟢 Low

**Виправлення:**
```javascript
function copyHistoryLink(btn, url) {
  navigator.clipboard.writeText(url).then(() => {
    var old = btn.textContent;
    btn.textContent = "✓ Скопійовано";
    btn.classList.add("ok");
    setTimeout(() => {
      btn.textContent = old;
      btn.classList.remove("ok");
    }, 2500);
  }).catch(() => {
    var a = document.createElement("textarea");
    a.value = url; a.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(a); a.select();
    document.execCommand("copy");
    document.body.removeChild(a);
    btn.textContent = "✓";
    setTimeout(() => { btn.textContent = "Копіювати"; }, 2000);
  });
}
```

---

### MGR-04 · useTemplate — немає scroll до форми після переходу
**Файл:** `manager.html`, `useTemplate()`
**Пріоритет:** 🟢 Low

**Виправлення — додати scroll:**
```javascript
function useTemplate(id, receiver_key, purpose, amount) {
  E("pay-receiver").value = receiver_key;
  E("pay-purpose").value = purpose;
  E("pay-amount").value = amount;
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
  document.querySelector("[data-t='pay']").classList.add("active");
  E("p-pay").classList.add("active");
  E("p-pay").scrollIntoView({ behavior: "smooth", block: "start" });
  toast("Шаблон завантажено — перевірте поля і створіть платіж");
}
```

---

## 4. Консистентність

### CON-01 · admin.html та manager.html не використовують `--primary-lt`, `--primary-bd`
**Файли:** `admin.html`, `manager.html`
**Пріоритет:** 🟡 Medium

**Поточний стан:** Ці змінні оголошені тільки в `templates.py` → `pay_page_html`. В admin і manager таких змінних немає — при потребі вони не будуть доступні для кастомних елементів.

**Виправлення — додати до `:root` обох файлів:**
```css
:root {
  --bg: #F8F9FB;
  --card: #FFFFFF;
  --blue: #1D6F42;
  --blue-dk: #155733;
  --red: #D92D20;
  --green: #1D6F42;
  --text: #101828;
  --muted: #667085;
  --border: #EAECF0;
  --r: 12px;
  --sh: 0 1px 2px rgba(16,24,40,.04), 0 4px 16px rgba(16,24,40,.06);
  /* Додати: */
  --primary-lt: #F0FAF4;
  --primary-bd: #A9D6B8;
  --danger: #D92D20;
  --t: .17s ease;
}
```

---

### CON-02 · Іконки змішані — emoji і SVG без системи
**Файли:** `templates.py