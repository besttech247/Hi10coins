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
# 🗄️ تهيئة قاعدة بيانات SQLite تلقائياً عند بدء التشغيل
# =====================================================================
DB_FILE = "bot_data.db"
ACTIVE_SESSIONS = set()  # حفظ معرفات الجلسات النشطة في الذاكرة

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. جدول المستخدمين
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )
    """)

    # 2. جدول إعدادات البوتات
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT UNIQUE NOT NULL,
        api_key TEXT DEFAULT '',
        api_secret TEXT DEFAULT '',
        paper_trading INTEGER DEFAULT 1,
        initial_capital REAL DEFAULT 500.0,
        trade_size_usdt REAL DEFAULT 10.0,
        max_concurrent_per_coin INTEGER DEFAULT 5,
        stop_loss_pct REAL DEFAULT 0.0049,
        daily_target_per_coin REAL DEFAULT 1.50,
        daily_target_portfolio REAL DEFAULT 5.00,
        status TEXT DEFAULT 'RUNNING'
    )
    """)

    # إنشاء مستخدم المدير الافتراضي إذا لم يكن موجوداً (admin / admin123)
    default_pass = hashlib.sha256("admin123".encode('utf-8')).hexdigest()
    cursor.execute("""
    INSERT OR IGNORE INTO users (id, username, password_hash)
    VALUES (1, 'admin', ?)
    """, (default_pass,))

    # إنشاء الإعدادات الافتراضية للبوت الأول (EWO Momentum Bot)
    cursor.execute("""
    INSERT OR IGNORE INTO bots_config (id, bot_name, paper_trading, initial_capital, trade_size_usdt, status)
    VALUES (1, 'EWO_MOMENTUM', 1, 500.0, 10.0, 'RUNNING')
    """)

    conn.commit()
    conn.close()
    print("✅ تم فحص وتهيئة قاعدة بيانات SQLite (bot_data.db) تلقائياً.")

init_db()

def get_bot_config(bot_name="EWO_MOMENTUM"):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bots_config WHERE bot_name = ?", (bot_name,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {}

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
# ⚙️ الإعدادات العامة
# =====================================================================
BASE_URL = "https://api.mexc.com"
PORT = 8080

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

PRECISION_MAP = {
    "NEARUSDT": 2, "AVAXUSDT": 2, "SOLUSDT": 2, "DOGEUSDT": 0, "BTCUSDT": 4,
    "ETHUSDT": 4, "BNBUSDT": 3, "XRPUSDT": 1, "ADAUSDT": 1, "LINKUSDT": 2
}

ssl_ctx = ssl._create_unverified_context()
CURRENT_CFG = get_bot_config("EWO_MOMENTUM")

bot_state = {
    "status": CURRENT_CFG.get("status", "RUNNING"),
    "paper_mode": bool(CURRENT_CFG.get("paper_trading", 1)),
    "virtual_balance": float(CURRENT_CFG.get("initial_capital", 500.0)),
    "real_balance": 0.0,
    "wallet_assets": [],
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "daily_pnl_portfolio": 0.0,
    "total_realized_pnl": 0.0,
    "total_trades_count": 0,
    "winning_trades_count": 0,
    "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
    "active_positions": {sym: [] for sym in SYMBOLS},
    "market_prices": {sym: {"bid": 0.0, "ask": 0.0} for sym in SYMBOLS},
    "api_connected": False,
    "recent_logs": []
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    bot_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(bot_state["recent_logs"]) > 100:
        bot_state["recent_logs"].pop()

# =====================================================================
# 🛠️ معالجة دقة الكسور والتداول
# =====================================================================
def fetch_exchange_precisions():
    try:
        url = f"{BASE_URL}/api/v3/exchangeInfo"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as res:
            data = json.loads(res.read().decode('utf-8'))
            for s in data.get("symbols", []):
                sym = s.get("symbol")
                if sym in SYMBOLS or sym.endswith("USDT"):
                    prec = s.get("baseAssetPrecision", 2)
                    for f in s.get("filters", []):
                        if f.get("filterType") == "LOT_SIZE":
                            step = float(f.get("stepSize", "0.01"))
                            if step > 0:
                                prec = max(0, int(round(-math.log10(step))))
                    PRECISION_MAP[sym] = prec
            add_log("تم جلب قواعد دقة الكسور العشرية من MEXC", "info")
    except Exception as e:
        add_log(f"استخدام دقة الكسور الاحتياطية: {e}", "warning")

def format_quantity(symbol, qty):
    prec = PRECISION_MAP.get(symbol, 2)
    factor = 10 ** prec
    truncated = math.floor(qty * factor) / factor
    return f"{int(truncated)}" if prec == 0 else f"{truncated:.{prec}f}"

# =====================================================================
# 🔐 وظائف MEXC API
# =====================================================================
def sign_query(query_string, secret):
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def mexc_private_request(endpoint, method="GET", params=None):
    cfg = get_bot_config("EWO_MOMENTUM")
    api_key = cfg.get("api_key", "").strip()
    api_secret = cfg.get("api_secret", "").strip()

    if not api_key or not api_secret:
        return False, "مفاتيح API غير محددة"
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query_string = urllib.parse.urlencode(params)
    signature = sign_query(query_string, api_secret)
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    
    headers = {
        "X-MEXC-APIKEY": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as res:
            return True, json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return False, f"خطأ HTTP {e.code}: {e.read().decode('utf-8')}"
    except Exception as e:
        return False, f"فشل الاتصال: {str(e)}"

def update_account_assets():
    ok, data = mexc_private_request("/api/v3/account", method="GET")
    if ok and "balances" in data:
        bot_state["api_connected"] = True
        assets = []
        for a in data["balances"]:
            free = float(a["free"])
            locked = float(a["locked"])
            total = free + locked
            if total > 0.00001:
                assets.append({"asset": a["asset"], "free": free, "locked": locked, "total": total})
                if a["asset"] == "USDT":
                    bot_state["real_balance"] = free
        bot_state["wallet_assets"] = assets
        return True
    bot_state["api_connected"] = False
    return False

def place_order(symbol, side, qty=None, quote_qty=None):
    cfg = get_bot_config("EWO_MOMENTUM")
    if cfg.get("paper_trading", 1) == 1:
        return True, {"status": "FILLED", "orderId": f"PAPER_{int(time.time()*1000)}"}
    
    params = {"symbol": symbol, "side": side.upper(), "type": "MARKET"}
    if side.upper() == "BUY" and quote_qty:
        params["quoteOrderQty"] = f"{quote_qty:.2f}"
    elif qty:
        params["quantity"] = format_quantity(symbol, qty)
    else:
        return False, "يجب تحديد الكمية أو القيمة"
    return mexc_private_request("/api/v3/order", method="POST", params=params)

def execute_buy(symbol, manual=False):
    cfg = get_bot_config("EWO_MOMENTUM")
    trade_size = float(cfg.get("trade_size_usdt", 10.0))
    is_paper = bool(cfg.get("paper_trading", 1))

    bid, ask = get_orderbook(symbol)
    if not ask or ask == 0:
        add_log(f"تعذر الشراء لـ {symbol}: السعر غير متوفر", "danger")
        return False, "السعر غير متوفر"
    
    avail_balance = bot_state["virtual_balance"] if is_paper else bot_state["real_balance"]
    if avail_balance < trade_size:
        msg = f"رصيد غير كافي ({avail_balance:.2f}$) لشراء {symbol}"
        add_log(msg, "warning")
        return False, msg

    raw_qty = trade_size / ask
    qty_str = format_quantity(symbol, raw_qty)
    qty = float(qty_str)
    
    if qty <= 0:
        msg = f"حجم الأوردر صغير جداً لـ {symbol}"
        add_log(msg, "warning")
        return False, msg

    ok, res = place_order(symbol, "BUY", qty=qty, quote_qty=trade_size)
    if ok:
        if is_paper:
            bot_state["virtual_balance"] -= trade_size
        bot_state["active_positions"][symbol].append({
            'entry_price': ask,
            'qty': qty,
            'time': datetime.now(timezone.utc).strftime("%H:%M:%S")
        })
        src = "يدوي ⚡" if manual else "تلقائي 🤖"
        mode_str = "تجريبي" if is_paper else "حقيقي"
        add_log(f"🚀 شراء {src} ({mode_str}) لـ {symbol} عند {ask}$ (الكمية: {qty_str})", "primary")
        return True, res
    else:
        add_log(f"❌ فشل شراء {symbol}: {res}", "danger")
        return False, res

# =====================================================================
# 📡 دوال السوق
# =====================================================================
def get_orderbook(symbol):
    try:
        url = f"{BASE_URL}/api/v3/ticker/bookTicker?symbol={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
            d = json.loads(res.read().decode('utf-8'))
            return float(d['bidPrice']), float(d['askPrice'])
    except Exception:
        return None, None

def fetch_klines(symbol):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=5m&limit=45"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=7) as res:
            data = json.loads(res.read().decode('utf-8'))
            return [{'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4])} for r in data]
    except Exception:
        return []

def calculate_ewo(candles):
    if len(candles) < 38:
        return None, None, None
    medians = [(c['high'] + c['low']) / 2.0 for c in candles]
    vals = []
    for offset in [3, 2, 1]:
        sub = medians[:len(candles) - offset + 1]
        sma5 = sum(sub[-5:]) / 5.0
        sma35 = sum(sub[-35:]) / 35.0
        vals.append(sma5 - sma35)
    return vals[0], vals[1], vals[2]

# =====================================================================
# 🔄 حلقة التداول الرئيسية
# =====================================================================
def trading_loop():
    fetch_exchange_precisions()
    add_log("تم بدء محرك التداول وقاعدة البيانات جاهزة", "info")
    
    while True:
        try:
            cfg = get_bot_config("EWO_MOMENTUM")
            is_paper = bool(cfg.get("paper_trading", 1))
            bot_state["paper_mode"] = is_paper
            bot_state["status"] = cfg.get("status", "RUNNING")

            if not is_paper and cfg.get("api_key") and cfg.get("api_secret"):
                update_account_assets()

            if bot_state["status"] == "STOPPED":
                time.sleep(3)
                continue

            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != bot_state["current_day"]:
                bot_state["current_day"] = now_day
                bot_state["daily_pnl_portfolio"] = 0.0
                bot_state["daily_pnl_coins"] = {sym: 0.0 for sym in SYMBOLS}
                add_log(f"🌅 بداية يوم جديد ({now_day} UTC) - تصفير الأهداف", "info")

            port_target_locked = bot_state["daily_pnl_portfolio"] >= float(cfg.get("daily_target_portfolio", 5.0))

            for sym in SYMBOLS:
                bid, ask = get_orderbook(sym)
                if bid and ask:
                    bot_state["market_prices"][sym] = {"bid": bid, "ask": ask}

                candles = fetch_klines(sym)
                if not candles or not bid:
                    continue

                e3, e2, e1 = calculate_ewo(candles)
                if e1 is None:
                    continue

                # 1. إدارة الصفقات المفتوحة
                still_open = []
                for pos in bot_state["active_positions"][sym]:
                    entry = pos['entry_price']
                    qty = pos['qty']
                    sl = entry * (1.0 - float(cfg.get("stop_loss_pct", 0.0049)))

                    if bid <= sl:
                        ok, res = place_order(sym, "SELL", qty=qty)
                        if ok:
                            pnl = (bid - entry) * qty
                            bot_state["virtual_balance"] += (float(cfg.get("trade_size_usdt", 10.0)) + pnl)
                            bot_state["daily_pnl_portfolio"] += pnl
                            bot_state["total_realized_pnl"] += pnl
                            bot_state["daily_pnl_coins"][sym] += pnl
                            bot_state["total_trades_count"] += 1
                            add_log(f"🛑 ضرب الوقف لـ {sym} عند {bid}$ (PnL: {pnl:+.3f}$)", "danger")
                        else:
                            still_open.append(pos)

                    elif (e2 > 0) and (e1 < e2):
                        ok, res = place_order(sym, "SELL", qty=qty)
                        if ok:
                            pnl = (ask - entry) * qty
                            bot_state["virtual_balance"] += (float(cfg.get("trade_size_usdt", 10.0)) + pnl)
                            bot_state["daily_pnl_portfolio"] += pnl
                            bot_state["total_realized_pnl"] += pnl
                            bot_state["daily_pnl_coins"][sym] += pnl
                            bot_state["total_trades_count"] += 1
                            bot_state["winning_trades_count"] += 1
                            add_log(f"🎯 جني أرباح EWO لـ {sym} عند {ask}$ (PnL: {pnl:+.3f}$)", "success")
                        else:
                            still_open.append(pos)
                    else:
                        still_open.append(pos)

                bot_state["active_positions"][sym] = still_open

                # 2. الشراء الآلي
                if bot_state["status"] == "RUNNING":
                    coin_target_locked = bot_state["daily_pnl_coins"][sym] >= float(cfg.get("daily_target_per_coin", 1.5))
                    sig_rebound = (e1 < 0) and (e1 > e2) and (e2 <= e3)
                    can_open = len(bot_state["active_positions"][sym]) < int(cfg.get("max_concurrent_per_coin", 5))

                    if sig_rebound and can_open and not port_target_locked and not coin_target_locked:
                        execute_buy(sym, manual=False)

        except Exception as e:
            add_log(f"تنبيه المحرك: {str(e)}", "warning")

        time.sleep(8)

# =====================================================================
# 🌐 واجهات HTML (صفحة الدخول + لوحة التحكم)
# =====================================================================
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>تسجيل الدخول - MEXC Hub</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--text:#f3f4f6;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);display:flex;align-items:center;justify-content:center;height:100vh;padding:14px}
.login-box{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;width:100%;max-width:380px;box-shadow:0 8px 24px #00000044}
h2{font-size:18px;margin-bottom:6px;text-align:center}
p{font-size:12px;color:var(--sub);text-align:center;margin-bottom:18px}
.input-group{margin-bottom:14px}
label{display:block;font-size:12px;color:var(--sub);margin-bottom:5px}
input{width:100%;padding:10px 12px;background:#090d16;border:1px solid var(--border);border-radius:8px;color:#fff;font-size:13px}
button{width:100%;padding:10px;background:var(--primary);color:#fff;border:none;border-radius:8px;font-weight:bold;cursor:pointer;font-size:14px;margin-top:8px}
button:hover{opacity:0.9}
.err{color:#ef4444;font-size:12px;text-align:center;margin-top:10px;display:none}
</style>
</head>
<body>
<div class="login-box">
  <h2>🔐 لوحة تحكم التداول الآلي</h2>
  <p>سجل الدخول لإدارة البوتات والمحفظة</p>
  <form id="login-form">
    <div class="input-group">
      <label>اسم المستخدم</label>
      <input type="text" id="username" placeholder="admin" required>
    </div>
    <div class="input-group">
      <label>كلمة المرور</label>
      <input type="password" id="password" placeholder="admin123" required>
    </div>
    <button type="submit">تسجيل الدخول</button>
    <div id="err-msg" class="err">بيانات الدخول غير صحيحة!</div>
  </form>
</div>
<script>
document.getElementById('login-form').addEventListener('submit', async(e)=>{
  e.preventDefault();
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      username: document.getElementById('username').value,
      password: document.getElementById('password').value
    })
  });
  if(res.ok){
    window.location.href = '/';
  } else {
    document.getElementById('err-msg').style.display = 'block';
  }
});
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MEXC Master Hub</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--warning:#f59e0b;--text:#f3f4f6;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:14px;line-height:1.5}
.container{max-width:1020px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:14px;background:var(--card);border-radius:12px;border:1px solid var(--border);margin-bottom:12px;flex-wrap:wrap;gap:10px}
.pill{padding:5px 12px;border-radius:20px;font-size:12px;font-weight:bold}
.pill-running{background:#10b98122;color:var(--success)}
.pill-paused{background:#f59e0b22;color:var(--warning)}
.pill-stopped{background:#ef444422;color:var(--danger)}
.btn-group{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn{padding:7px 13px;border:none;border-radius:8px;font-weight:bold;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:6px}
.btn-run{background:var(--success);color:#fff}
.btn-pause{background:var(--warning);color:#000}
.btn-stop{background:var(--danger);color:#fff}
.btn-logout{background:#334155;color:#fff}
.btn-buy{background:var(--primary);color:#fff;padding:4px 9px;font-size:12px}
.btn-panic{background:var(--danger);color:#fff;padding:4px 9px;font-size:12px}
.btn-copy{background:#334155;color:#fff;font-size:11px;padding:4px 8px}
.btn:hover{opacity:0.85}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px}
.card-title{font-size:12px;color:var(--sub);margin-bottom:4px}
.card-val{font-size:22px;font-weight:bold}
details{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:12px;overflow:hidden}
summary{padding:12px 16px;cursor:pointer;font-weight:bold;font-size:14px;display:flex;justify-content:space-between;align-items:center;background:#151e30}
summary:hover{background:#1a253c}
.details-content{padding:14px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:9px 12px;font-size:13px;border-bottom:1px solid var(--border)}
th{color:var(--sub)}
.badge{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:bold}
.badge-active{background:#10b98122;color:var(--success)}
.badge-idle{background:#64748b22;color:var(--sub)}
.logs{max-height:220px;overflow-y:auto;font-family:monospace;font-size:12px}
.log-row{padding:4px 0;border-bottom:1px solid #1f293d44}
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
input, select{background:#090d16;border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;width:100%}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h2 style="font-size:17px">🤖 MEXC Trader Master Hub (SQLite Auth)</h2>
      <p style="font-size:12px;color:var(--sub)">نظام التداول الآلي، قاعدة البيانات، وتسييل الأرصدة</p>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span id="bot-mode-pill" class="pill pill-running">جاري التشغيل</span>
      <div class="btn-group">
        <button class="btn btn-run" onclick="setBotStatus('RUNNING')">▶️ تشغيل</button>
        <button class="btn btn-pause" onclick="setBotStatus('PAUSED')">⏸️ مؤقت</button>
        <button class="btn btn-stop" onclick="setBotStatus('STOPPED')">⏹️ إيقاف</button>
        <button class="btn btn-logout" onclick="logout()">🚪 خروج</button>
      </div>
    </div>
  </div>

  <!-- 1. تعديل إعدادات البوت والـ API في قاعدة البيانات -->
  <details>
    <summary style="color:#60a5fa"><span>⚙️ إعدادات البوت والـ API (محفوظة في SQLite)</span><span>انقر للفتح ▾</span></summary>
    <div class="details-content">
      <div class="form-grid">
        <div>
          <label style="font-size:11px;color:var(--sub)">نمط التداول</label>
          <select id="cfg-mode">
            <option value="1">محاكاة تجريبية (Paper)</option>
            <option value="0">تداول حقيقي (Live API)</option>
          </select>
        </div>
        <div>
          <label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label>
          <input type="number" id="cfg-trade-size" value="10">
        </div>
        <div>
          <label style="font-size:11px;color:var(--sub)">رأس المال الافتراضي ($)</label>
          <input type="number" id="cfg-capital" value="500">
        </div>
      </div>
      <div class="form-grid" style="margin-top:10px">
        <div>
          <label style="font-size:11px;color:var(--sub)">MEXC API Key</label>
          <input type="password" id="cfg-key" placeholder="أدخل API Key">
        </div>
        <div>
          <label style="font-size:11px;color:var(--sub)">MEXC API Secret</label>
          <input type="password" id="cfg-secret" placeholder="أدخل API Secret">
        </div>
      </div>
      <button class="btn" style="background:var(--primary);color:#fff;margin-top:10px;width:100%;justify-content:center" onclick="saveSettings()">💾 حفظ الإعدادات في قاعدة البيانات</button>
    </div>
  </details>

  <!-- 2. محفظة الأرصدة والتسييل الطارئ -->
  <details open>
    <summary style="color:#38bdf8"><span>💰 أرصدة المحفظة والتسييل الطارئ للأرصدة المعلقة</span><span>انقر للطي ▴</span></summary>
    <div class="details-content">
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>العملة (Asset)</th><th>المتاح (Free)</th><th>المحجوز</th><th>الإجمالي</th><th>إجراء تسييل طارئ</th></tr>
          </thead>
          <tbody id="wallet-assets-body">
            <tr><td colspan="5" style="text-align:center;color:var(--sub)">جاري قراءة المحفظة...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </details>

  <div class="grid">
    <div class="card">
      <div class="card-title">الرصيد المتاح</div>
      <div class="card-val" id="balance-val">0.00$</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="balance-mode">جاري الفحص...</div>
    </div>
    <div class="card">
      <div class="card-title">أرباح اليوم المحققة</div>
      <div class="card-val" id="pnl-val">+0.00$</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px">الهدف: 5.00$</div>
    </div>
    <div class="card">
      <div class="card-title">إحصائيات الصفقات</div>
      <div class="card-val" id="win-rate">0.0%</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="trade-stats">0 صفقات منفذة</div>
    </div>
  </div>

  <!-- العملات العشر -->
  <div class="card" style="margin-bottom:14px;padding:0;overflow:hidden">
    <div style="padding:12px 16px;background:#151e30;font-weight:bold;font-size:14px">📊 مراقبة العملات العشر والشراء السريع</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>العملة</th><th>سعر السوق</th><th>ربح اليوم</th><th>المفتوح</th><th>أمر فوري</th></tr>
        </thead>
        <tbody id="coin-rows"></tbody>
      </table>
    </div>
  </div>

  <!-- الصفقات المفتوحة -->
  <details open>
    <summary><span>📂 الأوردرات والصفقات المفتوحة</span><span id="open-count-badge" class="badge badge-active">0 صفقات</span></summary>
    <div class="details-content table-wrap">
      <table>
        <thead>
          <tr><th>العملة</th><th>سعر الدخول</th><th>السعر الحالي</th><th>الكمية</th><th>وقف الخسارة</th><th>الوقت</th></tr>
        </thead>
        <tbody id="open-orders-body">
          <tr><td colspan="6" style="text-align:center;color:var(--sub)">لا توجد صفقات مفتوحة حالياً</td></tr>
        </tbody>
      </table>
    </div>
  </details>

  <!-- سجل العمليات مع زر النسخ -->
  <details open>
    <summary>
      <span>📜 سجل الأحداث والعمليات الفورية</span>
      <button class="btn btn-copy" onclick="event.stopPropagation(); copyLogs()">📋 نسخ السجلات</button>
    </summary>
    <div class="details-content">
      <div class="logs" id="log-box"></div>
    </div>
  </details>
</div>

<script>
let currentLogsText = "";

function copyLogs(){
  if(!currentLogsText){ alert("لا توجد سجلات لنسخها حالياً"); return; }
  navigator.clipboard.writeText(currentLogsText).then(() => {
    alert("✅ تم نسخ كامل السجلات إلى الحافظة!");
  }).catch(err => { alert("فشل النسخ: " + err); });
}

async function logout(){
  await fetch('/api/logout', {method: 'POST'});
  window.location.href = '/login';
}

async function saveSettings(){
  const payload = {
    paper_trading: parseInt(document.getElementById('cfg-mode').value),
    trade_size_usdt: parseFloat(document.getElementById('cfg-trade-size').value) || 10,
    initial_capital: parseFloat(document.getElementById('cfg-capital').value) || 500,
    api_key: document.getElementById('cfg-key').value,
    api_secret: document.getElementById('cfg-secret').value
  };
  await fetch('/api/save_config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  alert('✅ تم حفظ الإعدادات في قاعدة بيانات SQLite بنجاح!');
  refreshDashboard();
}

async function panicSellAsset(asset){
  if(confirm(`هل أنت متأكد من تسييل وبيع كامل رصيدك من ${asset} بسعر السوق؟`)){
    const res = await fetch('/api/panic_sell', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({asset: asset})
    });
    const data = await res.json();
    alert(data.msg || JSON.stringify(data));
    refreshDashboard();
  }
}

async function setBotStatus(status){
  await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: status})
  });
  refreshDashboard();
}

async function manualBuy(symbol){
  if(confirm(`هل تريد إرسال إشارة شراء فورية لـ ${symbol}؟`)){
    const res = await fetch('/api/manual_buy', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol: symbol})
    });
    const result = await res.json();
    if(!result.success){
      alert('خطأ أثناء الشراء: ' + JSON.stringify(result.details));
    }
    refreshDashboard();
  }
}

async function refreshDashboard(){
  try{
    const res = await fetch('/api/data');
    if(res.status === 401){ window.location.href = '/login'; return; }
    const d = await res.json();
    
    const pill = document.getElementById('bot-mode-pill');
    if(d.status === 'RUNNING'){ pill.className = 'pill pill-running'; pill.innerText = 'جاري التشغيل'; }
    else if(d.status === 'PAUSED'){ pill.className = 'pill pill-paused'; pill.innerText = 'إيقاف مؤقت'; }
    else { pill.className = 'pill pill-stopped'; pill.innerText = 'متوقف'; }

    const bal = d.paper_mode ? d.virtual_balance : d.real_balance;
    document.getElementById('balance-val').innerText = bal.toFixed(2) + '$';
    
    const balMode = document.getElementById('balance-mode');
    if(d.paper_mode){
      balMode.innerText = 'رصيد محاكاة افتراضي';
      balMode.style.color = 'var(--sub)';
    } else if(d.api_connected){
      balMode.innerText = '🟢 رصيد MEXC الحقيقي (متصل)';
      balMode.style.color = 'var(--success)';
    } else {
      balMode.innerText = '🔴 API غير متصل أو بدون رصيد';
      balMode.style.color = 'var(--danger)';
    }

    const pnl = d.daily_pnl_portfolio;
    const pnlEl = document.getElementById('pnl-val');
    pnlEl.innerText = (pnl>=0?'+':'') + pnl.toFixed(3) + '$';
    pnlEl.style.color = pnl>=0?'var(--success)':'var(--danger)';

    const totalT = d.total_trades_count;
    const winT = d.winning_trades_count;
    document.getElementById('win-rate').innerText = totalT > 0 ? ((winT/totalT)*100).toFixed(1) + '%' : '0.0%';
    document.getElementById('trade-stats').innerText = `${totalT} صفقة (${winT} رابحة)`;

    // جدول المحفظة
    let walletHtml = '';
    if(d.wallet_assets && d.wallet_assets.length > 0){
      d.wallet_assets.forEach(a => {
        const canSell = a.asset !== 'USDT' && a.free > 0;
        walletHtml += `<tr>
          <td><strong>${a.asset}</strong></td>
          <td>${a.free}</td>
          <td>${a.locked}</td>
          <td>${a.total}</td>
          <td>${canSell ? `<button class="btn btn-panic" onclick="panicSellAsset('${a.asset}')">🔥 تسييل وبيع فوري</button>` : `<span style="color:var(--sub)">-</span>`}</td>
        </tr>`;
      });
    } else {
      walletHtml = '<tr><td colspan="5" style="text-align:center;color:var(--sub)">لا توجد أرصدة متوفرة أو أن الحساب في الوضع التجريبي</td></tr>';
    }
    document.getElementById('wallet-assets-body').innerHTML = walletHtml;

    // مراقبة العملات
    let rowsHtml = '';
    let openOrdersHtml = '';
    let totalOpen = 0;

    for(const sym of Object.keys(d.market_prices)){
      const positions = d.active_positions[sym] || [];
      const count = positions.length;
      totalOpen += count;
      const coinPnl = d.daily_pnl_coins[sym] || 0;
      const price = d.market_prices[sym] ? d.market_prices[sym].bid : 0;
      
      rowsHtml += `<tr>
        <td><strong>${sym}</strong></td>
        <td>${price ? price.toFixed(4)+'$' : '-'}</td>
        <td style="color:${coinPnl>=0?'var(--success)':'var(--danger)'};font-weight:bold">${(coinPnl>=0?'+':'')+coinPnl.toFixed(3)}$</td>
        <td><span class="badge ${count>0?'badge-active':'badge-idle'}">${count}/5</span></td>
        <td><button class="btn btn-buy" onclick="manualBuy('${sym}')">⚡ شراء فوري</button></td>
      </tr>`;

      positions.forEach(pos => {
        const curPrice = d.market_prices[sym]?.bid || pos.entry_price;
        const slPrice = pos.entry_price * (1 - 0.0049);
        openOrdersHtml += `<tr>
          <td><strong>${sym}</strong></td>
          <td>${pos.entry_price.toFixed(4)}$</td>
          <td>${curPrice.toFixed(4)}$</td>
          <td>${pos.qty}</td>
          <td style="color:var(--danger)">${slPrice.toFixed(4)}$</td>
          <td>${pos.time}</td>
        </tr>`;
      });
    }
    document.getElementById('coin-rows').innerHTML = rowsHtml;
    document.getElementById('open-count-badge').innerText = `${totalOpen} صفقات`;
    if(openOrdersHtml){
      document.getElementById('open-orders-body').innerHTML = openOrdersHtml;
    } else {
      document.getElementById('open-orders-body').innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--sub)">لا توجد صفقات مفتوحة حالياً</td></tr>';
    }

    let logHtml = '';
    let logRawText = '';
    for(const l of d.recent_logs){
      logHtml += `<div class="log-row"><span style="color:var(--sub)">[${l.time}]</span> <span>${l.msg}</span></div>`;
      logRawText += `[${l.time}] ${l.msg}\n`;
    }
    currentLogsText = logRawText;
    document.getElementById('log-box').innerHTML = logHtml || '<div style="color:var(--sub)">في انتظار الأحداث...</div>';
  }catch(e){}
}
setInterval(refreshDashboard, 2000);
refreshDashboard();
</script>
</body>
</html>"""

# =====================================================================
# 🛡️ خادم الويب وإدارة الجلسات والصلاحيات
# =====================================================================
class AuthenticatedServer(http.server.BaseHTTPRequestHandler):
    def is_authenticated(self):
        cookie_header = self.headers.get('Cookie')
        if not cookie_header:
            return False
        c = cookies.SimpleCookie(cookie_header)
        session_id = c.get('session_id')
        if session_id and session_id.value in ACTIVE_SESSIONS:
            return True
        return False

    def do_GET(self):
        if self.path == '/login':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(LOGIN_HTML.encode('utf-8'))
            return

        if not self.is_authenticated():
            self.send_response(302)
            self.send_header('Location', '/login')
            self.end_headers()
            return

        if self.path == '/api/data':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(bot_state, ensure_ascii=False).encode('utf-8'))
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data.decode('utf-8')) if content_length > 0 else {}

        if self.path == '/api/login':
            username = data.get("username", "")
            password = data.get("password", "")
            if verify_user(username, password):
                session_token = secrets.token_hex(24)
                ACTIVE_SESSIONS.add(session_token)
                
                self.send_response(200)
                self.send_header('Set-Cookie', f'session_id={session_token}; Path=/; HttpOnly; SameSite=Lax')
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
            else:
                self.send_response(401)
                self.end_headers()
            return

        if not self.is_authenticated():
            self.send_response(401)
            self.end_headers()
            return

        if self.path == '/api/logout':
            cookie_header = self.headers.get('Cookie')
            if cookie_header:
                c = cookies.SimpleCookie(cookie_header)
                s = c.get('session_id')
                if s and s.value in ACTIVE_SESSIONS:
                    ACTIVE_SESSIONS.remove(s.value)
            self.send_response(200)
            self.send_header('Set-Cookie', 'session_id=; Path=/; Max-Age=0')
            self.end_headers()

        elif self.path == '/api/save_config':
            updates = {}
            if "paper_trading" in data: updates["paper_trading"] = int(data["paper_trading"])
            if "trade_size_usdt" in data: updates["trade_size_usdt"] = float(data["trade_size_usdt"])
            if "initial_capital" in data: updates["initial_capital"] = float(data["initial_capital"])
            if data.get("api_key"): updates["api_key"] = data["api_key"].strip()
            if data.get("api_secret"): updates["api_secret"] = data["api_secret"].strip()
            
            update_bot_config("EWO_MOMENTUM", updates)
            add_log("تم تحديث إعدادات البوت في قاعدة بيانات SQLite", "info")
            self.send_response(200)
            self.end_headers()

        elif self.path == '/api/control':
            new_status = data.get("status", "RUNNING")
            update_bot_config("EWO_MOMENTUM", {"status": new_status})
            bot_state["status"] = new_status
            add_log(f"تم تغيير حالة البوت إلى: {new_status}", "info")
            self.send_response(200)
            self.end_headers()

        elif self.path == '/api/panic_sell':
            asset = data.get("asset")
            if not asset or asset == "USDT":
                self.send_response(400); self.end_headers(); return

            symbol = f"{asset}USDT"
            free_qty = 0.0
            for a in bot_state.get("wallet_assets", []):
                if a["asset"] == asset: free_qty = a["free"]; break
            
            if free_qty <= 0:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({"msg": f"لا يوجد رصيد متاح من {asset}"}, ensure_ascii=False).encode('utf-8'))
                return

            ok, res = place_order(symbol, "SELL", qty=free_qty)
            if ok:
                add_log(f"🔥 تسييل طارئ ناجح لـ {asset}: تم بيع {free_qty} إلى USDT", "success")
                bot_state["active_positions"][symbol] = []
                update_account_assets()
                msg = f"✅ تم تسييل كامل رصيد {asset} ({free_qty}) بنجاح!"
            else:
                add_log(f"❌ فشل التسييل لـ {asset}: {res}", "danger")
                msg = f"❌ فشل التسييل: {res}"

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"msg": msg, "details": res}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/manual_buy':
            sym = data.get("symbol")
            if sym and sym in SYMBOLS:
                ok, res = execute_buy(sym, manual=True)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({"success": ok, "details": res}, ensure_ascii=False).encode('utf-8'))
            else:
                self.send_response(400)
                self.end_headers()

    def log_message(self, format, *args):
        return

def run_web_server():
    with socketserver.TCPServer(("", PORT), AuthenticatedServer) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    print("="*65)
    print(f"🚀 تم تشغيل الخادم ولوحة التحكم على: http://127.0.0.1:{PORT}")
    print(f"🔑 تسجيل الدخول: admin / admin123")
    print("="*65)
    
    t_server = threading.Thread(target=run_web_server, daemon=True)
    t_server.start()
    
    trading_loop()
