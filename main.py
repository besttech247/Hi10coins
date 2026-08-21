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
from datetime import datetime, timezone

# =====================================================================
# 📁 قراءة ملف config.json الآمن محلياً
# =====================================================================
def load_config():
    default_config = {
        "PAPER_TRADING": True,
        "MEXC_API_KEY": "",
        "MEXC_API_SECRET": "",
        "INITIAL_CAPITAL": 500.0,
        "TRADE_SIZE_USDT": 10.0,
        "MAX_CONCURRENT_PER_COIN": 5,
        "STOP_LOSS_PCT": 0.0049,
        "DAILY_TARGET_PER_COIN": 1.50,
        "DAILY_TARGET_PORTFOLIO": 5.00
    }
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                default_config.update(json.load(f))
        except Exception as e:
            print(f"خطأ في قراءة config.json: {e}")
    else:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
    return default_config

CFG = load_config()

CONFIG = {
    "paper_trading": CFG.get("PAPER_TRADING", True),
    "api_key": CFG.get("MEXC_API_KEY", "").strip(),
    "api_secret": CFG.get("MEXC_API_SECRET", "").strip(),
    "trade_size_usdt": float(CFG.get("TRADE_SIZE_USDT", 10.0)),
    "initial_capital": float(CFG.get("INITIAL_CAPITAL", 500.0)),
    "max_concurrent_per_coin": int(CFG.get("MAX_CONCURRENT_PER_COIN", 5)),
    "stop_loss_pct": float(CFG.get("STOP_LOSS_PCT", 0.0049)),
    "daily_target_per_coin": float(CFG.get("DAILY_TARGET_PER_COIN", 1.50)),
    "daily_target_portfolio": float(CFG.get("DAILY_TARGET_PORTFOLIO", 5.00))
}

BASE_URL = "https://api.mexc.com"
PORT = 8080

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

ssl_ctx = ssl._create_unverified_context()

# =====================================================================
# 📊 حالة النظام ولوحة التحكم
# =====================================================================
bot_state = {
    "status": "RUNNING",
    "paper_mode": CONFIG["paper_trading"],
    "virtual_balance": CONFIG["initial_capital"],
    "real_balance": 0.0,
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "daily_pnl_portfolio": 0.0,
    "total_realized_pnl": 0.0,
    "total_trades_count": 0,
    "winning_trades_count": 0,
    "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
    "active_positions": {sym: [] for sym in SYMBOLS},
    "market_prices": {sym: {"bid": 0.0, "ask": 0.0} for sym in SYMBOLS},
    "api_connected": False,
    "api_key_set": bool(CONFIG["api_key"] and CONFIG["api_secret"]),
    "recent_logs": []
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    bot_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(bot_state["recent_logs"]) > 100:
        bot_state["recent_logs"].pop()

# =====================================================================
# 🔐 وظائف MEXC API المتقدمة والتشخيص
# =====================================================================
def sign_query(query_string, secret):
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def mexc_private_request(endpoint, method="GET", params=None):
    if not CONFIG["api_key"] or not CONFIG["api_secret"]:
        return False, "مفاتيح API_KEY أو API_SECRET غير محددة في config.json"
    if params is None:
        params = {}
    
    params["timestamp"] = int(time.time() * 1000)
    query_string = urllib.parse.urlencode(params)
    signature = sign_query(query_string, CONFIG["api_secret"])
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    
    headers = {
        "X-MEXC-APIKEY": CONFIG["api_key"],
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as res:
            data = json.loads(res.read().decode('utf-8'))
            return True, data
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8')
        return False, f"خطأ HTTP {e.code}: {err_body}"
    except Exception as e:
        return False, f"فشل الاتصال: {str(e)}"

def check_mexc_health():
    """فحص شامل للاتصال والمفاتيح والتوقيت"""
    report = {}
    # 1. فحص Ping
    t0 = time.time()
    try:
        req = urllib.request.Request(f"{BASE_URL}/api/v3/ping", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
            report["ping_ms"] = round((time.time() - t0) * 1000, 2)
            report["server_reachable"] = True
    except Exception as e:
        report["server_reachable"] = False
        report["ping_error"] = str(e)
        return report

    # 2. فحص مطابقة التوقيت
    try:
        req = urllib.request.Request(f"{BASE_URL}/api/v3/time", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
            srv_time = json.loads(res.read().decode('utf-8'))['serverTime']
            local_time = int(time.time() * 1000)
            report["time_drift_ms"] = local_time - srv_time
    except Exception:
        report["time_drift_ms"] = "غير معروف"

    # 3. فحص الحساب والرصيد
    if CONFIG["api_key"] and CONFIG["api_secret"]:
        ok, res = mexc_private_request("/api/v3/account", method="GET")
        if ok and "balances" in res:
            report["auth_success"] = True
            report["can_trade"] = res.get("canTrade", False)
            usdt_bal = next((float(a["free"]) for a in res["balances"] if a["asset"] == "USDT"), 0.0)
            report["usdt_balance"] = usdt_bal
            bot_state["real_balance"] = usdt_bal
            bot_state["api_connected"] = True
        else:
            report["auth_success"] = False
            report["auth_error"] = res
            bot_state["api_connected"] = False
    else:
        report["auth_success"] = False
        report["auth_error"] = "المفاتيح فارغة"

    return report

def fetch_real_balance():
    ok, data = mexc_private_request("/api/v3/account", method="GET")
    if ok and "balances" in data:
        bot_state["api_connected"] = True
        for asset in data["balances"]:
            if asset["asset"] == "USDT":
                bot_state["real_balance"] = float(asset["free"])
                return float(asset["free"])
    bot_state["api_connected"] = False
    return 0.0

def place_order(symbol, side, qty=None, quote_qty=None):
    if CONFIG["paper_trading"]:
        return True, {"status": "FILLED", "orderId": f"PAPER_{int(time.time()*1000)}"}
    
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET"
    }
    # الشراء الفوري في MEXC يفضل تحديده بالقيمة quoteOrderQty (USDT)
    if side.upper() == "BUY" and quote_qty:
        params["quoteOrderQty"] = f"{quote_qty:.2f}"
    elif qty:
        params["quantity"] = f"{qty:.4f}"
    else:
        return False, "يجب تحديد الكمية أو القيمة بالدولار"

    return mexc_private_request("/api/v3/order", method="POST", params=params)

def execute_buy(symbol, manual=False):
    bid, ask = get_orderbook(symbol)
    if not ask or ask == 0:
        add_log(f"تعذر تنفيذ الشراء لـ {symbol}: لم يتم جلب سعر السوق", "danger")
        return False, "سعر السوق غير متوفر"
    
    avail_balance = bot_state["virtual_balance"] if CONFIG["paper_trading"] else bot_state["real_balance"]
    if avail_balance < CONFIG["trade_size_usdt"]:
        msg = f"رصيد غير كافي ({avail_balance:.2f}$) لشراء {symbol} بحجم {CONFIG['trade_size_usdt']}$"
        add_log(msg, "warning")
        return False, msg

    buy_price = ask
    qty = round(CONFIG["trade_size_usdt"] / buy_price, 4)
    
    ok, res = place_order(symbol, "BUY", qty=qty, quote_qty=CONFIG["trade_size_usdt"])
    
    if ok:
        if CONFIG["paper_trading"]:
            bot_state["virtual_balance"] -= CONFIG["trade_size_usdt"]
        
        bot_state["active_positions"][symbol].append({
            'entry_price': buy_price,
            'qty': qty,
            'time': datetime.now(timezone.utc).strftime("%H:%M:%S")
        })
        src = "يدوي ⚡" if manual else "تلقائي 🤖"
        mode_str = "تجريبي" if CONFIG["paper_trading"] else "حقيقي"
        add_log(f"🚀 شراء {src} ({mode_str}) لـ {symbol} عند {buy_price}$ (الكمية: {qty})", "primary")
        return True, res
    else:
        add_log(f"❌ فشل تنفيذ الشراء لـ {symbol}: {res}", "danger")
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
    add_log("تم تشغيل محرك التداول", "info")
    while True:
        try:
            if not CONFIG["paper_trading"] and CONFIG["api_key"] and CONFIG["api_secret"]:
                fetch_real_balance()

            if bot_state["status"] == "STOPPED":
                time.sleep(3)
                continue

            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != bot_state["current_day"]:
                bot_state["current_day"] = now_day
                bot_state["daily_pnl_portfolio"] = 0.0
                bot_state["daily_pnl_coins"] = {sym: 0.0 for sym in SYMBOLS}
                add_log(f"🌅 يوم جديد ({now_day} UTC) - تصفير الأهداف اليومية", "info")

            port_target_locked = bot_state["daily_pnl_portfolio"] >= CONFIG["daily_target_portfolio"]

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

                # 1. متابعة الصفقات المفتوحة وإغلاقها
                still_open = []
                for pos in bot_state["active_positions"][sym]:
                    entry = pos['entry_price']
                    qty = pos['qty']
                    sl = entry * (1.0 - CONFIG["stop_loss_pct"])

                    if bid <= sl:
                        ok, res = place_order(sym, "SELL", qty=qty)
                        if ok:
                            pnl = (bid - entry) * qty
                            bot_state["virtual_balance"] += (CONFIG["trade_size_usdt"] + pnl)
                            bot_state["daily_pnl_portfolio"] += pnl
                            bot_state["total_realized_pnl"] += pnl
                            bot_state["daily_pnl_coins"][sym] += pnl
                            bot_state["total_trades_count"] += 1
                            add_log(f"🛑 ضرب الوقف لـ {sym} عند {bid}$ (PnL: {pnl:+.3f}$)", "danger")
                        else:
                            add_log(f"فشل إغلاق صفقة الوقف لـ {sym}: {res}", "danger")
                            still_open.append(pos)

                    elif (e2 > 0) and (e1 < e2):
                        ok, res = place_order(sym, "SELL", qty=qty)
                        if ok:
                            pnl = (ask - entry) * qty
                            bot_state["virtual_balance"] += (CONFIG["trade_size_usdt"] + pnl)
                            bot_state["daily_pnl_portfolio"] += pnl
                            bot_state["total_realized_pnl"] += pnl
                            bot_state["daily_pnl_coins"][sym] += pnl
                            bot_state["total_trades_count"] += 1
                            bot_state["winning_trades_count"] += 1
                            add_log(f"🎯 جني أرباح EWO لـ {sym} عند {ask}$ (PnL: {pnl:+.3f}$)", "success")
                        else:
                            add_log(f"فشل إغلاق صفقة الربح لـ {sym}: {res}", "danger")
                            still_open.append(pos)
                    else:
                        still_open.append(pos)

                bot_state["active_positions"][sym] = still_open

                # 2. الشراء الآلي
                if bot_state["status"] == "RUNNING":
                    coin_target_locked = bot_state["daily_pnl_coins"][sym] >= CONFIG["daily_target_per_coin"]
                    sig_rebound = (e1 < 0) and (e1 > e2) and (e2 <= e3)
                    can_open = len(bot_state["active_positions"][sym]) < CONFIG["max_concurrent_per_coin"]

                    if sig_rebound and can_open and not port_target_locked and not coin_target_locked:
                        execute_buy(sym, manual=False)

        except Exception as e:
            add_log(f"تنبيه المحرك: {str(e)}", "warning")

        time.sleep(8)

# =====================================================================
# 🌐 واجهة التحكم والتفاعل
# =====================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MEXC Master Hub & Tester</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--warning:#f59e0b;--text:#f3f4f6;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:14px;line-height:1.5}
.container{max-width:1000px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:14px;background:var(--card);border-radius:12px;border:1px solid var(--border);margin-bottom:12px;flex-wrap:wrap;gap:10px}
.pill{padding:5px 12px;border-radius:20px;font-size:12px;font-weight:bold}
.pill-running{background:#10b98122;color:var(--success)}
.pill-paused{background:#f59e0b22;color:var(--warning)}
.pill-stopped{background:#ef444422;color:var(--danger)}
.btn-group{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:8px 14px;border:none;border-radius:8px;font-weight:bold;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:6px}
.btn-run{background:var(--success);color:#fff}
.btn-pause{background:var(--warning);color:#000}
.btn-stop{background:var(--danger);color:#fff}
.btn-test{background:#6366f1;color:#fff}
.btn-buy{background:var(--primary);color:#fff;padding:5px 10px;font-size:12px}
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
th,td{padding:10px 12px;font-size:13px;border-bottom:1px solid var(--border)}
th{color:var(--sub)}
.badge{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:bold}
.badge-active{background:#10b98122;color:var(--success)}
.badge-idle{background:#64748b22;color:var(--sub)}
.logs{max-height:220px;overflow-y:auto;font-family:monospace;font-size:12px}
.log-row{padding:5px 0;border-bottom:1px solid #1f293d44}
.test-panel{background:#090d16;padding:12px;border-radius:8px;border:1px solid var(--border);margin-top:10px;font-family:monospace;font-size:12px;white-space:pre-wrap;max-height:180px;overflow-y:auto}
.form-row{display:flex;gap:10px;margin-top:8px;flex-wrap:wrap}
input, select{background:#090d16;border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h2 style="font-size:17px">🤖 MEXC Trader Master & Diagnostic Hub</h2>
      <p style="font-size:12px;color:var(--sub)">نظام التداول، إدارة المخاطر، وفحص الـ API المباشر</p>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span id="bot-mode-pill" class="pill pill-running">جاري التشغيل</span>
      <div class="btn-group">
        <button class="btn btn-run" onclick="setBotStatus('RUNNING')">▶️ تشغيل</button>
        <button class="btn btn-pause" onclick="setBotStatus('PAUSED')">⏸️ مؤقت</button>
        <button class="btn btn-stop" onclick="setBotStatus('STOPPED')">⏹️ إيقاف</button>
      </div>
    </div>
  </div>

  <!-- تفاصيل الفحص والاختبار المتقدم -->
  <details>
    <summary style="color:#a5b4fc"><span>🧪 نافذة فحص الاتصال واختبار الأوردرات (API Testing Suite)</span><span>انقر للفتح ▾</span></summary>
    <div class="details-content">
      <p style="font-size:12px;color:var(--sub);margin-bottom:10px">استخدم هذه الأدوات للتحقق الفوري من صلاحية المفاتيح، الاتصال بخوادم MEXC، وتنفيذ أوردرات تجريبية مع إظهار تقرير الخطأ بدقة.</p>
      
      <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
        <button class="btn btn-test" onclick="runHealthCheck()">🔍 فحص الاتصال والمفاتيح (Health Check)</button>
      </div>

      <div class="form-row" style="align-items:center">
        <label style="font-size:12px">تنفيذ أمر تجريبي يدوي مخصص:</label>
        <select id="test-sym">
          <option value="DOGEUSDT">DOGEUSDT</option>
          <option value="SOLUSDT">SOLUSDT</option>
          <option value="ADAUSDT">ADAUSDT</option>
          <option value="XRPUSDT">XRPUSDT</option>
          <option value="NEARUSDT">NEARUSDT</option>
        </select>
        <select id="test-side">
          <option value="BUY">شراء (BUY)</option>
          <option value="SELL">بيع (SELL)</option>
        </select>
        <input type="number" id="test-amount" placeholder="القيمة بالدولار (مثال 6)" style="width:170px" value="6">
        <button class="btn btn-buy" onclick="sendCustomTestOrder()">⚡ إرسال الأوردر التجريبي</button>
      </div>

      <div class="test-panel" id="test-output">تقرير الاختبار سيظهر هنا...</div>
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
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="pnl-sub">الهدف: 5.00$</div>
    </div>
    <div class="card">
      <div class="card-title">إحصائيات الصفقات</div>
      <div class="card-val" id="win-rate">0.0%</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="trade-stats">0 صفقات منفذة</div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px;padding:0;overflow:hidden">
    <div style="padding:12px 16px;background:#151e30;font-weight:bold;font-size:14px">📊 مراقبة العملات العشر والشراء الفوري</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>العملة</th><th>سعر السوق</th><th>ربح اليوم</th><th>المفتوح</th><th>أمر فوري</th></tr>
        </thead>
        <tbody id="coin-rows"></tbody>
      </table>
    </div>
  </div>

  <details open>
    <summary><span>📂 الأوردرات والصفقات المفتوحة حالياً</span><span id="open-count-badge" class="badge badge-active">0 صفقات</span></summary>
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

  <details open>
    <summary><span>📜 سجل الأحداث والعمليات الفورية</span><span style="font-size:11px;color:var(--sub)">Live Events</span></summary>
    <div class="details-content">
      <div class="logs" id="log-box"></div>
    </div>
  </details>
</div>

<script>
async function runHealthCheck(){
  document.getElementById('test-output').innerText = 'جاري الاتصال والتحقق من منصة MEXC...';
  try{
    const res = await fetch('/api/health');
    const data = await res.json();
    document.getElementById('test-output').innerText = JSON.stringify(data, null, 2);
  }catch(e){
    document.getElementById('test-output').innerText = 'تعذر إجراء الفحص: ' + e;
  }
}

async function sendCustomTestOrder(){
  const sym = document.getElementById('test-sym').value;
  const side = document.getElementById('test-side').value;
  const amt = parseFloat(document.getElementById('test-amount').value) || 6;

  document.getElementById('test-output').innerText = `جاري إرسال أمر ${side} لـ ${sym}...`;
  try{
    const res = await fetch('/api/test_order', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol: sym, side: side, amount: amt})
    });
    const data = await res.json();
    document.getElementById('test-output').innerText = JSON.stringify(data, null, 2);
    refreshDashboard();
  }catch(e){
    document.getElementById('test-output').innerText = 'فشل تنفيذ الطلب: ' + e;
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
    const d = await res.json();
    
    const pill = document.getElementById('bot-mode-pill');
    if(d.status === 'RUNNING'){
      pill.className = 'pill pill-running';
      pill.innerText = 'جاري التشغيل';
    } else if(d.status === 'PAUSED'){
      pill.className = 'pill pill-paused';
      pill.innerText = 'إيقاف مؤقت';
    } else {
      pill.className = 'pill pill-stopped';
      pill.innerText = 'متوقف';
    }

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
      balMode.innerText = '🔴 مفاتيح API غير متصلة أو غير صالحة';
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
    for(const l of d.recent_logs){
      logHtml += `<div class="log-row"><span style="color:var(--sub)">[${l.time}]</span> <span>${l.msg}</span></div>`;
    }
    document.getElementById('log-box').innerHTML = logHtml || '<div style="color:var(--sub)">في انتظار الأحداث...</div>';
  }catch(e){}
}
setInterval(refreshDashboard, 2000);
refreshDashboard();
</script>
</body>
</html>"""

class DashboardServer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/data':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(bot_state, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/api/health':
            report = check_mexc_health()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(report, ensure_ascii=False).encode('utf-8'))
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data.decode('utf-8')) if content_length > 0 else {}

        if self.path == '/api/control':
            new_status = data.get("status", "RUNNING")
            bot_state["status"] = new_status
            add_log(f"تم تغيير حالة البوت إلى: {new_status}", "info")
            self.send_response(200)
            self.end_headers()

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

        elif self.path == '/api/test_order':
            sym = data.get("symbol", "DOGEUSDT")
            side = data.get("side", "BUY")
            amt = float(data.get("amount", 6.0))
            
            if side == "BUY":
                ok, res = place_order(sym, "BUY", quote_qty=amt)
            else:
                # بيع تجريبي لكمية صغيرة
                bid, _ = get_orderbook(sym)
                qty = round(amt / bid, 2) if bid else 1.0
                ok, res = place_order(sym, "SELL", qty=qty)
                
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok, "response": res}, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        return

def run_web_server():
    with socketserver.TCPServer(("", PORT), DashboardServer) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    print("="*65)
    print(f"🚀 تم تشغيل البوت ولوحة الاختبار على: http://127.0.0.1:{PORT}")
    print("="*65)
    
    t_server = threading.Thread(target=run_web_server, daemon=True)
    t_server.start()
    
    trading_loop()
