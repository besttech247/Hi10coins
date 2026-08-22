import http.server
import socketserver
import threading
import json
import urllib.request
import urllib.parse
import urllib.error
import ssl
import time
import hmac
import hashlib
import os
import math
import secrets
from http import cookies
from datetime import datetime, timezone
import sqlite3

# =====================================================================
# ⚙️ إعدادات المنفذ لبيئة Railway
# =====================================================================
PORT = int(os.environ.get("PORT", 8080))
BASE_URL = "https://api.mexc.com"
DB_FILE = "bot_data.db"
ACTIVE_SESSIONS = set()
ssl_ctx = ssl._create_unverified_context()

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

PRECISION_MAP = {
    "NEARUSDT": 2, "AVAXUSDT": 2, "SOLUSDT": 2, "DOGEUSDT": 0, "BTCUSDT": 4,
    "ETHUSDT": 4, "BNBUSDT": 3, "XRPUSDT": 1, "ADAUSDT": 1, "LINKUSDT": 2
}

# =====================================================================
# 🗄️ تهيئة قاعدة بيانات SQLite تلقائياً
# =====================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT UNIQUE NOT NULL,
        api_key TEXT DEFAULT '',
        api_secret TEXT DEFAULT '',
        paper_trading INTEGER DEFAULT 1,
        initial_capital REAL DEFAULT 500.0,
        trade_size_usdt REAL DEFAULT 10.0,
        status TEXT DEFAULT 'RUNNING'
    )
    """)

    # حساب المدير: admin / admin123
    default_pass = hashlib.sha256("admin123".encode('utf-8')).hexdigest()
    cursor.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (1, 'admin', ?)", (default_pass,))
    
    # تهيئة إعدادات البوتين
    cursor.execute("INSERT OR IGNORE INTO bots_config (id, bot_name, paper_trading, trade_size_usdt, status) VALUES (1, 'EWO_BOT', 1, 10.0, 'RUNNING')")
    cursor.execute("INSERT OR IGNORE INTO bots_config (id, bot_name, paper_trading, trade_size_usdt, status) VALUES (2, 'RSI_BOT', 1, 10.0, 'PAUSED')")

    conn.commit()
    conn.close()

init_db()

def get_bot_config(bot_name):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bots_config WHERE bot_name = ?", (bot_name,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}

def update_bot_config(bot_name, updates):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    fields = [f"{k} = ?" for k in updates.keys()]
    values = list(updates.values())
    values.append(bot_name)
    cursor.execute(f"UPDATE bots_config SET {', '.join(fields)} WHERE bot_name = ?", values)
    conn.commit()
    conn.close()

def verify_user(username, password):
    pass_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ? AND password_hash = ?", (username, pass_hash))
    user = cursor.fetchone()
    conn.close()
    return user is not None

# =====================================================================
# 📊 الحالة العامة المشتركة
# =====================================================================
shared_state = {
    "api_connected": False,
    "real_balance": 0.0,
    "wallet_assets": [],
    "market_prices": {sym: {"bid": 0.0, "ask": 0.0} for sym in SYMBOLS},
    "recent_logs": [],
    "ewo": {
        "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        "virtual_balance": 500.0,
        "daily_pnl_portfolio": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "active_positions": {sym: [] for sym in SYMBOLS}
    },
    "rsi": {
        "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        "virtual_balance": 500.0,
        "daily_pnl_portfolio": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "active_positions": {sym: [] for sym in SYMBOLS}
    }
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    shared_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(shared_state["recent_logs"]) > 100:
        shared_state["recent_logs"].pop()

# =====================================================================
# 🔐 محرك MEXC API ودقة الكسور
# =====================================================================
def sign_query(query_string, secret):
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def mexc_private_request(api_key, api_secret, endpoint, method="GET", params=None):
    if not api_key or not api_secret:
        return False, "مفاتيح API مفقودة"
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query_string = urllib.parse.urlencode(params)
    signature = sign_query(query_string, api_secret)
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {"X-MEXC-APIKEY": api_key, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as res:
            return True, json.loads(res.read().decode('utf-8'))
    except Exception as e:
        return False, str(e)

def format_quantity(symbol, qty):
    prec = PRECISION_MAP.get(symbol, 2)
    factor = 10 ** prec
    truncated = math.floor(qty * factor) / factor
    return f"{int(truncated)}" if prec == 0 else f"{truncated:.{prec}f}"

def get_orderbook(symbol):
    try:
        url = f"{BASE_URL}/api/v3/ticker/bookTicker?symbol={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
            d = json.loads(res.read().decode('utf-8'))
            return float(d['bidPrice']), float(d['askPrice'])
    except Exception:
        return None, None

def fetch_klines(symbol, limit=45):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=5m&limit={limit}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=7) as res:
            data = json.loads(res.read().decode('utf-8'))
            return [{'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4])} for r in data]
    except Exception:
        return []

def place_order(api_key, api_secret, symbol, side, qty=None, quote_qty=None, is_paper=True):
    if is_paper:
        return True, {"status": "FILLED"}
    params = {"symbol": symbol, "side": side.upper(), "type": "MARKET"}
    if side.upper() == "BUY" and quote_qty:
        params["quoteOrderQty"] = f"{quote_qty:.2f}"
    elif qty:
        params["quantity"] = format_quantity(symbol, qty)
    else:
        return False, "تحديد الكمية مطلوب"
    return mexc_private_request(api_key, api_secret, "/api/v3/order", method="POST", params=params)

# =====================================================================
# 🤖 منطق استراتيجيات التداول
# =====================================================================
def calculate_ewo(candles):
    if len(candles) < 38: return None, None, None
    medians = [(c['high'] + c['low']) / 2.0 for c in candles]
    vals = []
    for offset in [3, 2, 1]:
        sub = medians[:len(candles) - offset + 1]
        sma5 = sum(sub[-5:]) / 5.0
        sma35 = sum(sub[-35:]) / 35.0
        vals.append(sma5 - sma35)
    return vals[0], vals[1], vals[2]

def calculate_rsi(candles, period=14):
    if len(candles) < period + 1: return 50.0
    closes = [c['close'] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

def trading_engine_loop():
    add_log("تم تشغيل محرك التداول المزدوج (EWO + RSI)", "info")
    while True:
        try:
            cfg_ewo = get_bot_config("EWO_BOT")
            cfg_rsi = get_bot_config("RSI_BOT")

            # مزامنة رصيد المحفظة الحقيقي
            k, s = cfg_ewo.get("api_key", "").strip(), cfg_ewo.get("api_secret", "").strip()
            if k and s:
                ok, acc = mexc_private_request(k, s, "/api/v3/account")
                if ok and "balances" in acc:
                    shared_state["api_connected"] = True
                    shared_state["wallet_assets"] = [b for b in acc["balances"] if float(b["free"]) + float(b["locked"]) > 0.0001]
                    for b in shared_state["wallet_assets"]:
                        if b["asset"] == "USDT":
                            shared_state["real_balance"] = float(b["free"])
                else:
                    shared_state["api_connected"] = False

            # فحص السوق للعملات
            for sym in SYMBOLS:
                bid, ask = get_orderbook(sym)
                if bid and ask:
                    shared_state["market_prices"][sym] = {"bid": bid, "ask": ask}

                candles = fetch_klines(sym)
                if not candles or not bid: continue

                # --- دورة بوت EWO ---
                if cfg_ewo.get("status") != "STOPPED":
                    e3, e2, e1 = calculate_ewo(candles)
                    if e1 is not None:
                        is_p = bool(cfg_ewo.get("paper_trading", 1))
                        size = float(cfg_ewo.get("trade_size_usdt", 10.0))
                        
                        # متابعة الخروج
                        still = []
                        for pos in shared_state["ewo"]["active_positions"].get(sym, []):
                            sl = pos['entry_price'] * (1.0 - 0.0049)
                            if bid <= sl or ((e2 > 0) and (e1 < e2)):
                                ok, res = place_order(k, s, sym, "SELL", qty=pos['qty'], is_paper=is_p)
                                if ok:
                                    pnl = (bid - pos['entry_price']) * pos['qty']
                                    shared_state["ewo"]["virtual_balance"] += (size + pnl)
                                    shared_state["ewo"]["daily_pnl_portfolio"] += pnl
                                    shared_state["ewo"]["total_trades"] += 1
                                    if pnl > 0: shared_state["ewo"]["winning_trades"] += 1
                                    add_log(f"[EWO] بيع {sym} PnL: {pnl:+.3f}$", "success" if pnl>0 else "danger")
                                else: still.append(pos)
                            else: still.append(pos)
                        shared_state["ewo"]["active_positions"][sym] = still

                        # الدخول
                        if cfg_ewo.get("status") == "RUNNING" and len(still) < 3 and (e1 < 0 and e1 > e2 and e2 <= e3):
                            q = float(format_quantity(sym, size / ask))
                            if q > 0:
                                ok, res = place_order(k, s, sym, "BUY", qty=q, quote_qty=size, is_paper=is_p)
                                if ok:
                                    if is_p: shared_state["ewo"]["virtual_balance"] -= size
                                    shared_state["ewo"]["active_positions"][sym].append({'entry_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")})
                                    add_log(f"[EWO] 🚀 شراء {sym} عند {ask}$", "primary")

                # --- دورة بوت RSI ---
                if cfg_rsi.get("status") != "STOPPED":
                    rsi = calculate_rsi(candles)
                    is_p = bool(cfg_rsi.get("paper_trading", 1))
                    size = float(cfg_rsi.get("trade_size_usdt", 10.0))
                    
                    still_rsi = []
                    for pos in shared_state["rsi"]["active_positions"].get(sym, []):
                        sl = pos['entry_price'] * (1.0 - 0.008)
                        if bid <= sl or rsi >= 68:
                            ok, res = place_order(k, s, sym, "SELL", qty=pos['qty'], is_paper=is_p)
                            if ok:
                                pnl = (bid - pos['entry_price']) * pos['qty']
                                shared_state["rsi"]["virtual_balance"] += (size + pnl)
                                shared_state["rsi"]["daily_pnl_portfolio"] += pnl
                                shared_state["rsi"]["total_trades"] += 1
                                if pnl > 0: shared_state["rsi"]["winning_trades"] += 1
                                add_log(f"[RSI] بيع {sym} PnL: {pnl:+.3f}$", "success" if pnl>0 else "danger")
                            else: still_rsi.append(pos)
                        else: still_rsi.append(pos)
                    shared_state["rsi"]["active_positions"][sym] = still_rsi

                    if cfg_rsi.get("status") == "RUNNING" and len(still_rsi) < 2 and rsi <= 28:
                        q = float(format_quantity(sym, size / ask))
                        if q > 0:
                            ok, res = place_order(k, s, sym, "BUY", qty=q, quote_qty=size, is_paper=is_p)
                            if ok:
                                if is_p: shared_state["rsi"]["virtual_balance"] -= size
                                shared_state["rsi"]["active_positions"][sym].append({'entry_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")})
                                add_log(f"[RSI] ⚡ شراء تشبع {sym} (RSI:{rsi:.0f})", "primary")

        except Exception as e:
            add_log(f"تنبيه المحرك: {e}", "warning")
        time.sleep(8)

# =====================================================================
# 🌐 واجهات HTML الموحدة
# =====================================================================
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>دخول - Command Hub</title>
<style>
body{background:#090d16;color:#f3f4f6;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#111827;padding:24px;border-radius:12px;width:320px;border:1px solid #1f293d}
input{width:100%;padding:10px;margin:8px 0;background:#090d16;border:1px solid #1f293d;color:#fff;border-radius:6px;box-sizing:border-box}
button{width:100%;padding:10px;background:#3b82f6;color:#fff;border:none;border-radius:6px;font-weight:bold;cursor:pointer}
</style>
</head>
<body>
<div class="box">
  <h3 style="text-align:center;margin-bottom:12px">🔐 تسجيل الدخول</h3>
  <form id="f">
    <input type="text" id="u" placeholder="اسم المستخدم" required>
    <input type="password" id="p" placeholder="كلمة المرور" required>
    <button type="submit">دخول</button>
  </form>
</div>
<script>
document.getElementById('f').onsubmit=async(e)=>{
  e.preventDefault();
  const r=await fetch('/api/login',{method:'POST',body:JSON.stringify({username:u.value,password:p.value})});
  if(r.ok) location.href='/'; else alert('خطأ في بيانات الدخول');
};
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>MEXC Multi-Bot Hub</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f3f4f6;--sub:#94a3b8}
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui}
body{background:var(--bg);color:var(--text);padding:12px}
.tabs{display:flex;gap:6px;margin:12px 0}
.tab{padding:8px 14px;background:#151e30;border:1px solid var(--border);border-radius:8px;color:var(--sub);cursor:pointer;font-weight:bold}
.tab.active{background:var(--primary);color:#fff}
.tab-pane{display:none}
.tab-pane.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:10px}
.val{font-size:20px;font-weight:bold}
.btn{padding:5px 10px;border:none;border-radius:6px;font-weight:bold;cursor:pointer;font-size:12px}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:7px;border-bottom:1px solid var(--border);font-size:12px}
.logs{max-height:160px;overflow-y:auto;font-family:monospace;font-size:11px}
</style>
</head>
<body>
  <div style="display:flex;justify-content:space-between;align-items:center" class="card">
    <div><strong>🎛️ لوحة القيادة الموحدة (Single File)</strong></div>
    <div id="api-stat" style="font-size:12px;color:var(--sub)">جاري الاتصال...</div>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="showTab('t1', this)">🤖 EWO Momentum</button>
    <button class="tab" onclick="showTab('t2', this)">⚡ RSI Scalper</button>
    <button class="tab" onclick="showTab('t3', this)">💰 المحفظة والتسييل</button>
  </div>

  <!-- EWO -->
  <div id="t1" class="tab-pane active">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>الحالة: <strong id="ewo-st">RUNNING</strong></span>
      <div>
        <button class="btn" style="background:var(--success);color:#fff" onclick="setSt('EWO_BOT','RUNNING')">▶️ تشغيل</button>
        <button class="btn" style="background:#f59e0b;color:#000" onclick="setSt('EWO_BOT','PAUSED')">⏸️ إيقاف مؤقت</button>
      </div>
    </div>
    <div class="grid">
      <div class="card"><div style="font-size:11px;color:var(--sub)">الرصيد الافتراضي</div><div class="val" id="ewo-bal">0$</div></div>
      <div class="card"><div style="font-size:11px;color:var(--sub)">أرباح اليوم</div><div class="val" id="ewo-pnl">0$</div></div>
    </div>
    <div class="card">
      <strong style="font-size:12px">الصفقات المفتوحة (EWO):</strong>
      <div style="overflow-x:auto"><table id="ewo-orders"><thead><tr><th>العملة</th><th>سعر الدخول</th><th>الكمية</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- RSI -->
  <div id="t2" class="tab-pane">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>الحالة: <strong id="rsi-st">PAUSED</strong></span>
      <div>
        <button class="btn" style="background:var(--success);color:#fff" onclick="setSt('RSI_BOT','RUNNING')">▶️ تشغيل</button>
        <button class="btn" style="background:#f59e0b;color:#000" onclick="setSt('RSI_BOT','PAUSED')">⏸️ إيقاف مؤقت</button>
      </div>
    </div>
    <div class="grid">
      <div class="card"><div style="font-size:11px;color:var(--sub)">الرصيد الافتراضي</div><div class="val" id="rsi-bal">0$</div></div>
      <div class="card"><div style="font-size:11px;color:var(--sub)">أرباح اليوم</div><div class="val" id="rsi-pnl">0$</div></div>
    </div>
    <div class="card">
      <strong style="font-size:12px">الصفقات المفتوحة (RSI):</strong>
      <div style="overflow-x:auto"><table id="rsi-orders"><thead><tr><th>العملة</th><th>سعر الدخول</th><th>الكمية</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Wallet -->
  <div id="t3" class="tab-pane">
    <div class="card">
      <div style="overflow-x:auto"><table id="w-table"><thead><tr><th>العملة</th><th>المتاح</th><th>إجراء تسييل</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Logs -->
  <div class="card">
    <div style="font-size:12px;font-weight:bold;margin-bottom:6px">📜 سجل العمليات المباشر:</div>
    <div class="logs" id="logs"></div>
  </div>

<script>
function showTab(id, btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
async function setSt(b,s){
  await fetch('/api/control',{method:'POST',body:JSON.stringify({bot_name:b,status:s})});
  update();
}
async function update(){
  try{
    const res = await fetch('/api/data');
    if(res.status===401) location.href='/login';
    const d = await res.json();
    
    document.getElementById('api-stat').innerHTML = d.api_connected ? '<span style="color:var(--success)">🟢 MEXC متصل</span>' : '<span style="color:var(--danger)">🔴 API غير متصل</span>';

    // EWO
    document.getElementById('ewo-bal').innerText = d.ewo.virtual_balance.toFixed(2)+'$';
    document.getElementById('ewo-pnl').innerText = (d.ewo.daily_pnl_portfolio>=0?'+':'')+d.ewo.daily_pnl_portfolio.toFixed(3)+'$';
    let eHtml = '';
    for(let s in d.ewo.active_positions){
      d.ewo.active_positions[s].forEach(p=>{ eHtml += `<tr><td>${s}</td><td>${p.entry_price}$</td><td>${p.qty}</td></tr>`; });
    }
    document.getElementById('ewo-orders').querySelector('tbody').innerHTML = eHtml || '<tr><td colspan="3" style="text-align:center;color:var(--sub)">لا توجد صفقات</td></tr>';

    // RSI
    document.getElementById('rsi-bal').innerText = d.rsi.virtual_balance.toFixed(2)+'$';
    document.getElementById('rsi-pnl').innerText = (d.rsi.daily_pnl_portfolio>=0?'+':'')+d.rsi.daily_pnl_portfolio.toFixed(3)+'$';
    let rHtml = '';
    for(let s in d.rsi.active_positions){
      d.rsi.active_positions[s].forEach(p=>{ rHtml += `<tr><td>${s}</td><td>${p.entry_price}$</td><td>${p.qty}</td></tr>`; });
    }
    document.getElementById('rsi-orders').querySelector('tbody').innerHTML = rHtml || '<tr><td colspan="3" style="text-align:center;color:var(--sub)">لا توجد صفقات</td></tr>';

    // Wallet
    let wHtml = '';
    (d.wallet_assets||[]).forEach(a=>{
      if(a.free>0 && a.asset!=='USDT') wHtml += `<tr><td>${a.asset}</td><td>${a.free}</td><td><button class="btn" style="background:var(--danger);color:#fff" onclick="panic('${a.asset}')">🔥 تسييل</button></td></tr>`;
    });
    document.getElementById('w-table').querySelector('tbody').innerHTML = wHtml || '<tr><td colspan="3" style="text-align:center;color:var(--sub)">لا توجد عملات أو الحساب تجريبي</td></tr>';

    // Logs
    let lHtml = '';
    (d.recent_logs||[]).forEach(l=>{ lHtml += `<div>[${l.time}] ${l.msg}</div>`; });
    document.getElementById('logs').innerHTML = lHtml;
  }catch(e){}
}
setInterval(update, 2500);
update();
</script>
</body>
</html>"""

# =====================================================================
# 🛡️ خادم الويب
# =====================================================================
class WebHandler(http.server.BaseHTTPRequestHandler):
    def is_auth(self):
        c = cookies.SimpleCookie(self.headers.get('Cookie'))
        s = c.get('session_id')
        return s and s.value in ACTIVE_SESSIONS

    def do_GET(self):
        if self.path == '/login':
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            self.wfile.write(LOGIN_HTML.encode('utf-8'))
            return
        if not self.is_auth():
            self.send_response(302); self.send_header('Location', '/login'); self.end_headers(); return

        if self.path == '/api/data':
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(shared_state, ensure_ascii=False).encode('utf-8'))
        else:
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        data = json.loads(self.rfile.read(length).decode('utf-8')) if length > 0 else {}

        if self.path == '/api/login':
            if verify_user(data.get("username", ""), data.get("password", "")):
                token = secrets.token_hex(24)
                ACTIVE_SESSIONS.add(token)
                self.send_response(200)
                self.send_header('Set-Cookie', f'session_id={token}; Path=/; HttpOnly; SameSite=Lax')
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
            else:
                self.send_response(401); self.end_headers()
            return

        if not self.is_auth():
            self.send_response(401); self.end_headers(); return

        if self.path == '/api/control':
            update_bot_config(data.get("bot_name", "EWO_BOT"), {"status": data.get("status", "RUNNING")})
            self.send_response(200); self.end_headers()

    def log_message(self, format, *args): return

if __name__ == "__main__":
    print(f"🚀 بدء التشغيل على المنفذ: {PORT}")
    threading.Thread(target=trading_engine_loop, daemon=True).start()
    with socketserver.TCPServer(("", PORT), WebHandler) as srv:
        srv.serve_forever()
