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
import database

PORT = int(os.environ.get("PORT", 8080))
database.init_db()

BASE_URL = "https://api.mexc.com"
ACTIVE_SESSIONS = set()
ssl_ctx = ssl._create_unverified_context()
START_TIME = time.time()

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

PRECISION_MAP = {
    "NEARUSDT": 2, "AVAXUSDT": 2, "SOLUSDT": 2, "DOGEUSDT": 0, "BTCUSDT": 4,
    "ETHUSDT": 4, "BNBUSDT": 3, "XRPUSDT": 1, "ADAUSDT": 1, "LINKUSDT": 2
}

PRICE_PRECISION_MAP = {
    "NEARUSDT": 4, "AVAXUSDT": 2, "SOLUSDT": 2, "DOGEUSDT": 5, "BTCUSDT": 2,
    "ETHUSDT": 2, "BNBUSDT": 2, "XRPUSDT": 4, "ADAUSDT": 4, "LINKUSDT": 3
}

shared_state = {
    "api_connected": False,
    "real_balance_usdt": 0.0,
    "total_wallet_usd_value": 0.0,
    "wallet_assets": [],
    "market_prices": {sym: {"bid": 0.0, "ask": 0.0} for sym in SYMBOLS},
    "recent_logs": [],
    "start_timestamp": START_TIME,
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "bots": {
        "BOT_1": {
            "name": "🤖 Bot 1 (EWO 5m)",
            "status": "RUNNING",
            "daily_pnl": 0.0,
            "daily_target": 5.0,
            "daily_coin_target": 1.5,
            "trades_count": 0,
            "winning_count": 0,
            "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
            "active_positions": {sym: [] for sym in SYMBOLS}
        },
        "BOT_2": {
            "name": "⚡ Bot 2 (EWO Custom TF)",
            "status": "PAUSED",
            "daily_pnl": 0.0,
            "daily_target": 5.0,
            "daily_coin_target": 1.5,
            "trades_count": 0,
            "winning_count": 0,
            "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
            "active_positions": {sym: [] for sym in SYMBOLS}
        },
        "BOT_3": {
            "name": "🎯 Bot 3 (Manual Trigger + Auto Bracket)",
            "status": "RUNNING",
            "daily_pnl": 0.0,
            "daily_target": 5.0,
            "daily_coin_target": 1.5,
            "trades_count": 0,
            "winning_count": 0,
            "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
            "active_positions": {sym: [] for sym in SYMBOLS}
        }
    }
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    shared_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(shared_state["recent_logs"]) > 120:
        shared_state["recent_logs"].pop()

# =====================================================================
# 🔐 محرك MEXC API ودقة الكسور
# =====================================================================
def sign_query(query_string, secret):
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def mexc_private_request(endpoint, method="GET", params=None):
    keys = database.get_keys()
    api_key, api_secret = keys.get("api_key", ""), keys.get("api_secret", "")
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

def format_price(symbol, price):
    prec = PRICE_PRECISION_MAP.get(symbol, 4)
    return f"{price:.{prec}f}"

def get_orderbook(symbol):
    try:
        url = f"{BASE_URL}/api/v3/ticker/bookTicker?symbol={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
            d = json.loads(res.read().decode('utf-8'))
            return float(d['bidPrice']), float(d['askPrice'])
    except Exception:
        return None, None

def fetch_klines(symbol, interval="5m", limit=45):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=7) as res:
            data = json.loads(res.read().decode('utf-8'))
            return [{'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4])} for r in data]
    except Exception:
        return []

def place_order(symbol, side, qty=None, quote_qty=None, order_type="MARKET", price=None):
    params = {"symbol": symbol, "side": side.upper(), "type": order_type.upper()}
    if order_type.upper() == "LIMIT":
        if not price or not qty:
            return False, "يجب تحديد السعر والكمية لأمر LIMIT"
        params["timeInForce"] = "GTC"
        params["price"] = format_price(symbol, price)
        params["quantity"] = format_quantity(symbol, qty)
    else:
        if side.upper() == "BUY" and quote_qty:
            params["quoteOrderQty"] = f"{quote_qty:.2f}"
        elif qty:
            params["quantity"] = format_quantity(symbol, qty)
        else:
            return False, "تحديد الكمية مطلوب"
    return mexc_private_request("/api/v3/order", method="POST", params=params)

# =====================================================================
# 🤖 محرك التداول الحقيقي وحساب القيمة الفعلية
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

def trading_engine_loop():
    add_log("تم تشغيل محرك التداول المركزي ومراقبة المحفظة", "info")
    while True:
        try:
            # 1. تصفير الأهداف اليومية 00:00 UTC
            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != shared_state["current_day"]:
                shared_state["current_day"] = now_day
                for bKey in ["BOT_1", "BOT_2", "BOT_3"]:
                    shared_state["bots"][bKey]["daily_pnl"] = 0.0
                    shared_state["bots"][bKey]["daily_pnl_coins"] = {sym: 0.0 for sym in SYMBOLS}
                add_log(f"🌅 بداية يوم تداول جديد ({now_day} UTC) - تصفير الأهداف اليومية", "info")

            # 2. فحص أسعار العملات
            for sym in SYMBOLS:
                bid, ask = get_orderbook(sym)
                if bid and ask:
                    shared_state["market_prices"][sym] = {"bid": bid, "ask": ask}

            # 3. تحديث أرصدة المحفظة الحقيقية والقيمة بالدولار
            ok, acc = mexc_private_request("/api/v3/account")
            if ok and "balances" in acc:
                shared_state["api_connected"] = True
                assets = []
                usdt_free = 0.0
                total_val_usd = 0.0
                
                for b in acc["balances"]:
                    free = float(b["free"])
                    locked = float(b["locked"])
                    total = free + locked
                    asset = b["asset"]
                    
                    if total > 0.0001:
                        usd_price = 1.0 if asset == "USDT" else shared_state["market_prices"].get(f"{asset}USDT", {}).get("bid", 0.0)
                        val_usd = total * usd_price
                        total_val_usd += val_usd
                        assets.append({
                            "asset": asset,
                            "free": free,
                            "locked": locked,
                            "total": total,
                            "usd_price": usd_price,
                            "usd_value": val_usd
                        })
                    if asset == "USDT":
                        usdt_free = free

                shared_state["wallet_assets"] = assets
                shared_state["real_balance_usdt"] = usdt_free
                shared_state["total_wallet_usd_value"] = total_val_usd
            else:
                shared_state["api_connected"] = False

            cfg_b1 = database.get_bot_config("BOT_1")
            cfg_b2 = database.get_bot_config("BOT_2")
            cfg_b3 = database.get_bot_config("BOT_3")

            shared_state["bots"]["BOT_1"]["status"] = cfg_b1.get("status", "RUNNING")
            shared_state["bots"]["BOT_2"]["status"] = cfg_b2.get("status", "PAUSED")
            shared_state["bots"]["BOT_3"]["status"] = cfg_b3.get("status", "RUNNING")

            b1_target_locked = shared_state["bots"]["BOT_1"]["daily_pnl"] >= shared_state["bots"]["BOT_1"]["daily_target"]
            b2_target_locked = shared_state["bots"]["BOT_2"]["daily_pnl"] >= shared_state["bots"]["BOT_2"]["daily_target"]

            for sym in SYMBOLS:
                bid = shared_state["market_prices"][sym]["bid"]
                ask = shared_state["market_prices"][sym]["ask"]
                if not bid: continue

                # --- Bot 1 (EWO 5m) ---
                if cfg_b1.get("status") != "STOPPED":
                    candles_b1 = fetch_klines(sym, interval="5m", limit=45)
                    if candles_b1:
                        e3, e2, e1 = calculate_ewo(candles_b1)
                        if e1 is not None:
                            size = float(cfg_b1.get("trade_size_usdt", 10.0))
                            sl_pct = float(cfg_b1.get("sl_pct", 0.0049))

                            still_b1 = []
                            for pos in shared_state["bots"]["BOT_1"]["active_positions"].get(sym, []):
                                sl = pos['entry_price'] * (1.0 - sl_pct)
                                if bid <= sl or ((e2 > 0) and (e1 < e2)):
                                    ok, res = place_order(sym, "SELL", qty=pos['qty'])
                                    if ok:
                                        pnl = (bid - pos['entry_price']) * pos['qty']
                                        shared_state["bots"]["BOT_1"]["daily_pnl"] += pnl
                                        shared_state["bots"]["BOT_1"]["daily_pnl_coins"][sym] += pnl
                                        shared_state["bots"]["BOT_1"]["trades_count"] += 1
                                        if pnl > 0: shared_state["bots"]["BOT_1"]["winning_count"] += 1
                                        add_log(f"[Bot 1] بيع {sym} PnL: {pnl:+.3f}$", "success" if pnl > 0 else "danger")
                                    else: still_b1.append(pos)
                                else: still_b1.append(pos)
                            shared_state["bots"]["BOT_1"]["active_positions"][sym] = still_b1

                            b1_coin_locked = shared_state["bots"]["BOT_1"]["daily_pnl_coins"].get(sym, 0.0) >= shared_state["bots"]["BOT_1"]["daily_coin_target"]
                            can_open = len(still_b1) < 5
                            sig_rebound = (e1 < 0 and e1 > e2 and e2 <= e3)

                            if cfg_b1.get("status") == "RUNNING" and can_open and sig_rebound and not b1_target_locked and not b1_coin_locked:
                                if shared_state["real_balance_usdt"] >= size:
                                    q = float(format_quantity(sym, size / ask))
                                    if q > 0:
                                        ok, res = place_order(sym, "BUY", qty=q, quote_qty=size)
                                        if ok:
                                            shared_state["bots"]["BOT_1"]["active_positions"][sym].append({
                                                'id': f"b1_{int(time.time()*1000)}", 'entry_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")
                                            })
                                            add_log(f"[Bot 1] 🚀 شراء {sym} عند {ask}$ ({len(still_b1)+1}/5)", "primary")

                # --- Bot 2 (EWO Custom TF) ---
                if cfg_b2.get("status") != "STOPPED":
                    tf = cfg_b2.get("timeframe", "15m")
                    candles_b2 = fetch_klines(sym, interval=tf, limit=45)
                    if candles_b2:
                        e3, e2, e1 = calculate_ewo(candles_b2)
                        if e1 is not None:
                            size2 = float(cfg_b2.get("trade_size_usdt", 10.0))
                            sl_pct2 = float(cfg_b2.get("sl_pct", 0.006))

                            still_b2 = []
                            for pos in shared_state["bots"]["BOT_2"]["active_positions"].get(sym, []):
                                sl = pos['entry_price'] * (1.0 - sl_pct2)
                                if bid <= sl or ((e2 > 0) and (e1 < e2)):
                                    ok, res = place_order(sym, "SELL", qty=pos['qty'])
                                    if ok:
                                        pnl = (bid - pos['entry_price']) * pos['qty']
                                        shared_state["bots"]["BOT_2"]["daily_pnl"] += pnl
                                        shared_state["bots"]["BOT_2"]["daily_pnl_coins"][sym] += pnl
                                        shared_state["bots"]["BOT_2"]["trades_count"] += 1
                                        if pnl > 0: shared_state["bots"]["BOT_2"]["winning_count"] += 1
                                        add_log(f"[Bot 2 ({tf})] بيع {sym} PnL: {pnl:+.3f}$", "success" if pnl > 0 else "danger")
                                    else: still_b2.append(pos)
                                else: still_b2.append(pos)
                            shared_state["bots"]["BOT_2"]["active_positions"][sym] = still_b2

                            b2_coin_locked = shared_state["bots"]["BOT_2"]["daily_pnl_coins"].get(sym, 0.0) >= shared_state["bots"]["BOT_2"]["daily_coin_target"]
                            can_open2 = len(still_b2) < 5
                            sig_rebound2 = (e1 < 0 and e1 > e2 and e2 <= e3)

                            if cfg_b2.get("status") == "RUNNING" and can_open2 and sig_rebound2 and not b2_target_locked and not b2_coin_locked:
                                if shared_state["real_balance_usdt"] >= size2:
                                    q = float(format_quantity(sym, size2 / ask))
                                    if q > 0:
                                        ok, res = place_order(sym, "BUY", qty=q, quote_qty=size2)
                                        if ok:
                                            shared_state["bots"]["BOT_2"]["active_positions"][sym].append({
                                                'id': f"b2_{int(time.time()*1000)}", 'entry_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")
                                            })
                                            add_log(f"[Bot 2 ({tf})] ⚡ شراء {sym} عند {ask}$ ({len(still_b2)+1}/5)", "primary")

                # --- Bot 3 (Manual Trigger + Auto Bracket & Trailing) ---
                if cfg_b3.get("status") != "STOPPED":
                    tp_pct = float(cfg_b3.get("tp_pct", 0.015))
                    sl_pct = float(cfg_b3.get("sl_pct", 0.005))
                    use_ts = bool(cfg_b3.get("trailing_stop", 1))
                    cb_pct = float(cfg_b3.get("trailing_cb", 0.003))

                    still_b3 = []
                    for pos in shared_state["bots"]["BOT_3"]["active_positions"].get(sym, []):
                        entry = pos['entry_price']
                        highest = pos.get('highest_price', entry)
                        
                        if bid > highest:
                            highest = bid
                            pos['highest_price'] = highest

                        tp_price = entry * (1.0 + tp_pct)
                        sl_price = entry * (1.0 - sl_pct)
                        
                        trailing_sl_price = highest * (1.0 - cb_pct) if use_ts else sl_price
                        effective_sl = max(sl_price, trailing_sl_price) if use_ts and highest >= (entry * (1.0 + cb_pct)) else sl_price

                        hit_tp = bid >= tp_price
                        hit_sl = bid <= effective_sl

                        if hit_tp or hit_sl:
                            ok, res = place_order(sym, "SELL", qty=pos['qty'])
                            if ok:
                                pnl = (bid - entry) * pos['qty']
                                shared_state["bots"]["BOT_3"]["daily_pnl"] += pnl
                                shared_state["bots"]["BOT_3"]["daily_pnl_coins"][sym] += pnl
                                shared_state["bots"]["BOT_3"]["trades_count"] += 1
                                if pnl > 0: shared_state["bots"]["BOT_3"]["winning_count"] += 1
                                reason = "🎯 جني أرباح TP" if hit_tp else ("🔄 تريلينج ستوب" if use_ts and effective_sl > sl_price else "🛑 وقف خسارة SL")
                                add_log(f"[Bot 3] {reason} لـ {sym} PnL: {pnl:+.3f}$", "success" if pnl > 0 else "danger")
                            else:
                                still_b3.append(pos)
                        else:
                            still_b3.append(pos)

                    shared_state["bots"]["BOT_3"]["active_positions"][sym] = still_b3

        except Exception as e:
            add_log(f"خطأ المحرك: {e}", "warning")

        time.sleep(7)

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
  if(r.ok) location.href='/'; else alert('بيانات الدخول غير صحيحة');
};
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Multi-Bot Command Hub</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f3f4f6;--sub:#94a3b8}
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:12px;line-height:1.5}
.header-box{display:flex;justify-content:space-between;align-items:center;padding:12px;background:var(--card);border-radius:12px;border:1px solid var(--border);margin-bottom:12px;flex-wrap:wrap;gap:10px}
.wallet-bar{display:flex;gap:12px;align-items:center;background:#151e30;padding:8px 12px;border-radius:8px;border:1px solid var(--border)}
.tabs{display:flex;gap:6px;margin:12px 0;overflow-x:auto}
.tab{padding:8px 14px;background:#151e30;border:1px solid var(--border);border-radius:8px;color:var(--sub);cursor:pointer;font-weight:bold;white-space:nowrap}
.tab.active{background:var(--primary);color:#fff}
.tab-pane{display:none}
.tab-pane.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:10px}
.card-title{font-size:12px;color:var(--sub);margin-bottom:4px}
.card-val{font-size:22px;font-weight:bold}
.progress-bar{height:6px;background:#1f293d;border-radius:3px;margin-top:8px;overflow:hidden}
.progress-fill{height:100%;background:var(--primary);transition:width .4s}
.btn{padding:5px 10px;border:none;border-radius:6px;font-weight:bold;cursor:pointer;font-size:12px}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:8px 10px;border-bottom:1px solid var(--border);font-size:12px}
th{color:var(--sub)}
.badge{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:bold}
.badge-active{background:#10b98122;color:var(--success)}
.badge-idle{background:#64748b22;color:var(--sub)}
.logs{max-height:180px;overflow-y:auto;font-family:monospace;font-size:11px}
input,select{background:#090d16;border:1px solid var(--border);color:#fff;padding:6px 10px;border-radius:6px;font-size:12px;width:100%}
details{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;overflow:hidden}
summary{padding:10px;cursor:pointer;font-weight:bold;background:#151e30}
.form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;padding:10px}
.badge-live{background:#10b98122;color:#10b981;border:1px solid #10b98144;padding:2px 6px;border-radius:4px;font-size:11px}
.action-cell{display:flex;gap:4px;align-items:center}
</style>
</head>
<body>
  <div class="header-box">
    <div>
      <div style="display:flex;align-items:center;gap:8px">
        <strong>🎛️ MEXC Multi-Bot Command Hub</strong>
        <span class="badge-live">⚡ تداول حقيقي 100%</span>
      </div>
      <div style="font-size:11px;color:var(--sub);margin-top:2px">⏳ عمر تشغيل البوت: <span id="uptime" style="color:#60a5fa;font-weight:bold">00:00:00</span></div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <div class="wallet-bar">
        <div><span style="font-size:11px;color:var(--sub)">USDT المتاح:</span> <strong id="live-usdt" style="color:#10b981;font-size:15px">0.00 $</strong></div>
        <div style="border-right:1px solid var(--border);padding-right:10px"><span style="font-size:11px;color:var(--sub)">إجمالي المحفظة:</span> <strong id="live-total-usd" style="color:#38bdf8;font-size:15px">0.00 $</strong></div>
      </div>
      <span id="api-stat" style="font-size:12px">جاري الفحص...</span>
      <button class="btn" style="background:#334155;color:#fff" onclick="fetch('/api/logout').then(()=>location.href='/login')">🚪 خروج</button>
    </div>
  </div>

  <!-- مفاتيح المنصة وكلمة المرور -->
  <details>
    <summary style="color:#60a5fa">🔑 إعدادات MEXC API وكلمة المرور ▾</summary>
    <div style="padding:10px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <input type="password" id="m-key" placeholder="MEXC API Key">
        <input type="password" id="m-sec" placeholder="MEXC API Secret">
      </div>
      <button class="btn" style="background:var(--primary);color:#fff;width:100%;margin-bottom:12px" onclick="saveKeys()">💾 حفظ المفاتيح</button>
      
      <hr style="border-color:var(--border);margin-bottom:8px">
      <strong style="font-size:12px;display:block;margin-bottom:4px">🔒 تغيير كلمة المرور:</strong>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <input type="password" id="new-pass" placeholder="كلمة المرور الجديدة">
        <button class="btn" style="background:#10b981;color:#fff" onclick="changePass()">تحديث كلمة المرور</button>
      </div>
    </div>
  </details>

  <div class="tabs">
    <button class="tab active" onclick="showTab('t1', this)">🤖 Bot 1 (EWO 5m)</button>
    <button class="tab" onclick="showTab('t2', this)">⚡ Bot 2 (EWO مخصص)</button>
    <button class="tab" onclick="showTab('t3', this)">🎯 Bot 3 (شراء يدوي + آلي)</button>
    <button class="tab" onclick="showTab('t4', this)">💰 المحفظة والتسييل</button>
  </div>

  <!-- Bot 1 (EWO 5m) -->
  <div id="t1" class="tab-pane active">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>حالة Bot 1: <strong id="b1-st" style="color:#10b981">RUNNING</strong></span>
      <div style="display:flex;gap:6px">
        <button class="btn" style="background:var(--success);color:#fff" onclick="setSt('BOT_1','RUNNING')">▶️ تشغيل</button>
        <button class="btn" style="background:#f59e0b;color:#000" onclick="setSt('BOT_1','PAUSED')">⏸️ إيقاف مؤقت</button>
        <button class="btn" style="background:var(--danger);color:#fff" onclick="setSt('BOT_1','STOPPED')">⏹️ إيقاف تام</button>
      </div>
    </div>
    
    <div class="grid">
      <div class="card">
        <div class="card-title">أرباح اليوم المحققة</div>
        <div class="card-val" id="b1-pnl">+0.00$</div>
        <div class="progress-bar"><div class="progress-fill" id="b1-pnl-bar" style="width:0%"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:4px">
          <span>الهدف: 5.00$</span><span id="b1-pnl-pct">0%</span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">نسبة الفوز الإجمالية</div>
        <div class="card-val" id="b1-winrate">0.0%</div>
        <div style="font-size:11px;color:var(--sub);margin-top:4px" id="b1-trade-stats">0 صفقات منفذة</div>
      </div>
      <div class="card">
        <div class="card-title">الصفقات والسيولة المفتوحة</div>
        <div class="card-val" id="b1-open-count">0 صفقات</div>
        <div style="font-size:11px;color:var(--sub);margin-top:4px" id="b1-cap-used">0$ محجوزة في السوق</div>
      </div>
    </div>

    <!-- إعدادات Bot 1 -->
    <details>
      <summary style="color:#a78bfa">⚙️ تخصيص حجم الصفقة ونسبة الوقف (Bot 1) ▾</summary>
      <div class="form-row">
        <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="b1-size" value="10"></div>
        <div><label style="font-size:11px;color:var(--sub)">وقف الخسارة (SL %)</label><input type="number" id="b1-sl" value="0.49" step="0.01"></div>
        <div style="display:flex;align-items:flex-end"><button class="btn" style="background:var(--primary);color:#fff;width:100%" onclick="saveBotCfg('BOT_1', 'b1')">💾 حفظ الإعدادات</button></div>
      </div>
    </details>

    <div class="card">
      <strong style="font-size:13px;display:block;margin-bottom:6px">📊 حالة العملات العشر، أرباح اليوم، والشراء السريع:</strong>
      <div style="overflow-x:auto">
        <table id="b1-coins-table">
          <thead><tr><th>العملة</th><th>سعر السوق</th><th>ربح اليوم</th><th>الصفقات المفتوحة</th><th>شراء فوري</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <strong style="font-size:13px;display:block;margin-bottom:6px">📂 صفقات Bot 1 المفتوحة:</strong>
      <div style="overflow-x:auto"><table id="b1-orders"><thead><tr><th>العملة</th><th>سعر الدخول</th><th>الكمية</th><th>الوقت</th><th>تسييل بأمر</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Bot 2 (EWO Custom TF) -->
  <div id="t2" class="tab-pane">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>حالة Bot 2: <strong id="b2-st" style="color:#f59e0b">PAUSED</strong></span>
      <div style="display:flex;gap:6px">
        <button class="btn" style="background:var(--success);color:#fff" onclick="setSt('BOT_2','RUNNING')">▶️ تشغيل</button>
        <button class="btn" style="background:#f59e0b;color:#000" onclick="setSt('BOT_2','PAUSED')">⏸️ إيقاف مؤقت</button>
        <button class="btn" style="background:var(--danger);color:#fff" onclick="setSt('BOT_2','STOPPED')">⏹️ إيقاف تام</button>
      </div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-title">أرباح اليوم المحققة</div>
        <div class="card-val" id="b2-pnl">+0.00$</div>
        <div class="progress-bar"><div class="progress-fill" id="b2-pnl-bar" style="width:0%"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:4px">
          <span>الهدف: 5.00$</span><span id="b2-pnl-pct">0%</span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">نسبة الفوز الإجمالية</div>
        <div class="card-val" id="b2-winrate">0.0%</div>
        <div style="font-size:11px;color:var(--sub);margin-top:4px" id="b2-trade-stats">0 صفقات منفذة</div>
      </div>
      <div class="card">
        <div class="card-title">الصفقات والسيولة المفتوحة</div>
        <div class="card-val" id="b2-open-count">0 صفقات</div>
        <div style="font-size:11px;color:var(--sub);margin-top:4px" id="b2-cap-used">0$ محجوزة في السوق</div>
      </div>
    </div>

    <!-- إعدادات Bot 2 -->
    <details>
      <summary style="color:#a78bfa">⚙️ تخصيص الفريم الزمني والصفقة (Bot 2) ▾</summary>
      <div class="form-row">
        <div>
          <label style="font-size:11px;color:var(--sub)">الفريم الزمني</label>
          <select id="b2-tf">
            <option value="1m">1 دقيقة</option>
            <option value="5m">5 دقائق</option>
            <option value="15m" selected>15 دقيقة</option>
            <option value="30m">30 دقيقة</option>
            <option value="60m">1 ساعة</option>
            <option value="4h">4 ساعات</option>
          </select>
        </div>
        <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="b2-size" value="10"></div>
        <div><label style="font-size:11px;color:var(--sub)">وقف الخسارة (SL %)</label><input type="number" id="b2-sl" value="0.6" step="0.01"></div>
        <div style="display:flex;align-items:flex-end"><button class="btn" style="background:var(--primary);color:#fff;width:100%" onclick="saveBotCfg('BOT_2', 'b2')">💾 حفظ الإعدادات</button></div>
      </div>
    </details>

    <div class="card">
      <strong style="font-size:13px;display:block;margin-bottom:6px">📊 جدول العملات وأرباح اليوم (Bot 2):</strong>
      <div style="overflow-x:auto"><table id="b2-coins-table"><thead><tr><th>العملة</th><th>سعر السوق</th><th>ربح اليوم</th><th>الصفقات المفتوحة</th><th>شراء فوري</th></tr></thead><tbody></tbody></table></div>
    </div>

    <div class="card">
      <strong style="font-size:13px;display:block;margin-bottom:6px">📂 صفقات Bot 2 المفتوحة:</strong>
      <div style="overflow-x:auto"><table id="b2-orders"><thead><tr><th>العملة</th><th>سعر الدخول</th><th>الكمية</th><th>الوقت</th><th>تسييل بأمر</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Bot 3 (Manual Trigger + Bracket) -->
  <div id="t3" class="tab-pane">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>حالة Bot 3: <strong id="b3-st" style="color:#10b981">RUNNING</strong></span>
      <div style="display:flex;gap:6px">
        <button class="btn" style="background:var(--success);color:#fff" onclick="setSt('BOT_3','RUNNING')">▶️ تشغيل</button>
        <button class="btn" style="background:#f59e0b;color:#000" onclick="setSt('BOT_3','PAUSED')">⏸️ إيقاف مؤقت</button>
      </div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-title">أرباح اليوم المحققة</div>
        <div class="card-val" id="b3-pnl">+0.00$</div>
        <div class="progress-bar"><div class="progress-fill" id="b3-pnl-bar" style="width:0%"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:4px">
          <span>الهدف: 5.00$</span><span id="b3-pnl-pct">0%</span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">نسبة الفوز الإجمالية</div>
        <div class="card-val" id="b3-winrate">0.0%</div>
        <div style="font-size:11px;color:var(--sub);margin-top:4px" id="b3-trade-stats">0 صفقات منفذة</div>
      </div>
      <div class="card">
        <div class="card-title">الصفقات والسيولة المفتوحة</div>
        <div class="card-val" id="b3-open-count">0 صفقات</div>
        <div style="font-size:11px;color:var(--sub);margin-top:4px" id="b3-cap-used">0$ محجوزة في السوق</div>
      </div>
    </div>

    <!-- إعدادات Bot 3 -->
    <details open>
      <summary style="color:#38bdf8">🎯 إعدادات جني الأرباح ووقف الخسارة التلقائي والـ Trailing Stop ▾</summary>
      <div class="form-row">
        <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="b3-size" value="10"></div>
        <div><label style="font-size:11px;color:var(--sub)">جني الأرباح (TP %)</label><input type="number" id="b3-tp" value="1.5" step="0.1"></div>
        <div><label style="font-size:11px;color:var(--sub)">وقف الخسارة (SL %)</label><input type="number" id="b3-sl" value="0.5" step="0.1"></div>
        <div>
          <label style="font-size:11px;color:var(--sub)">Trailing Stop</label>
          <select id="b3-ts"><option value="1">مفعّل ✅</option><option value="0">معطّل ❌</option></select>
        </div>
        <div style="display:flex;align-items:flex-end"><button class="btn" style="background:var(--primary);color:#fff;width:100%" onclick="saveBotCfg('BOT_3', 'b3')">💾 حفظ القواعد</button></div>
      </div>
    </details>

    <div class="card">
      <strong style="font-size:13px;display:block;margin-bottom:6px">⚡ إطلاق صفقة شراء حقيقية (يستلمها البوت فوراً بأوامر البيع الآلية):</strong>
      <div style="overflow-x:auto"><table id="b3-market-table"><thead><tr><th>العملة</th><th>سعر السوق</th><th>إطلاق الصفقة بنقرة واحدة</th></tr></thead><tbody></tbody></table></div>
    </div>

    <div class="card">
      <strong style="font-size:13px;display:block;margin-bottom:6px">📂 صفقات Bot 3 المفتوحة (تدار آلياً):</strong>
      <div style="overflow-x:auto"><table id="b3-orders"><thead><tr><th>العملة</th><th>سعر الدخول</th><th>أعلى سعر</th><th>الكمية</th><th>الوقت</th><th>تسييل يدوي</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Wallet & Panic Orders -->
  <div id="t4" class="tab-pane">
    <div class="card">
      <strong style="font-size:13px;display:block;margin-bottom:8px">💼 أرصدة المحفظة الحية والقيمة الفعلية بالدولار:</strong>
      <div style="overflow-x:auto">
        <table id="w-table">
          <thead>
            <tr>
              <th>العملة</th>
              <th>المتاح (Free)</th>
              <th>المحجوز (Locked)</th>
              <th>السعر اللحظي</th>
              <th>القيمة الفعلية ($)</th>
              <th>إجراء التسييل</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Logs -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <strong style="font-size:13px">📜 سجل العمليات الفورية (Live Events):</strong>
      <button class="btn" style="background:#334155;color:#fff;font-size:11px" onclick="copyLogs()">📋 نسخ السجلات</button>
    </div>
    <div class="logs" id="logs"></div>
  </div>

<script>
let logsText = "";
let startTs = Date.now();

function showTab(id, btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
function copyLogs(){
  navigator.clipboard.writeText(logsText).then(()=>alert("✅ تم نسخ السجلات إلى الحافظة!"));
}
async function setSt(b,s){
  await fetch('/api/control',{method:'POST',body:JSON.stringify({bot_name:b,status:s})});
  update();
}
async function saveKeys(){
  await fetch('/api/save_keys',{method:'POST',body:JSON.stringify({api_key:document.getElementById('m-key').value,api_secret:document.getElementById('m-sec').value})});
  alert('✅ تم حفظ مفاتيح MEXC بنجاح!');
  update();
}
async function changePass(){
  const p = document.getElementById('new-pass').value;
  if(!p){ alert("يرجى إدخال كلمة المرور الجديدة"); return; }
  const res = await fetch('/api/change_password', {method:'POST', body:JSON.stringify({new_password:p})});
  if(res.ok){ alert("✅ تم تغيير كلمة المرور بنجاح!"); document.getElementById('new-pass').value = ''; }
}
async function saveBotCfg(botName, prefix){
  const payload = {
    bot_name: botName,
    trade_size_usdt: parseFloat(document.getElementById(prefix+'-size').value)||10
  };
  if(prefix==='b1' || prefix==='b2'){
    payload.sl_pct = (parseFloat(document.getElementById(prefix+'-sl').value)||0.5) / 100.0;
  }
  if(prefix==='b2'){
    payload.timeframe = document.getElementById('b2-tf').value;
  }
  if(prefix==='b3'){
    payload.tp_pct = (parseFloat(document.getElementById('b3-tp').value)||1.5) / 100.0;
    payload.sl_pct = (parseFloat(document.getElementById('b3-sl').value)||0.5) / 100.0;
    payload.trailing_stop = parseInt(document.getElementById('b3-ts').value);
  }
  await fetch('/api/save_bot_config', {method:'POST', body:JSON.stringify(payload)});
  alert(`✅ تم حفظ إعدادات ${botName} بنجاح!`);
  update();
}
async function triggerBuy(botName, sym){
  if(confirm(`إطلاق صفقة شراء حقيقية لـ ${sym} عبر ${botName}؟`)){
    const r = await fetch('/api/manual_buy', {method:'POST', body:JSON.stringify({bot_name:botName, symbol:sym})});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}
async function closeSinglePos(botName, sym, posId){
  if(confirm(`تسييل صفقة ${sym} بأمر بيع فوري في المنصة؟`)){
    const r = await fetch('/api/close_position', {method:'POST', body:JSON.stringify({bot_name:botName, symbol:sym, pos_id:posId})});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}

// تسييل بسعر السوق
async function panicMarket(asset){
  if(confirm(`تسييل كامل رصيد ${asset} فورياً بسعر السوق (Market Order)؟`)){
    const r = await fetch('/api/panic',{method:'POST',body:JSON.stringify({asset:asset, order_type:'MARKET'})});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}

// تسييل بأمر معلق محدد السعر
async function panicLimit(asset, curPrice){
  const priceStr = prompt(`أدخل سعر البيع المطلوب لأمر LIMIT لعملة ${asset} (السعر اللحظي الحالي: ${curPrice}$):`, curPrice);
  if(priceStr){
    const limitPrice = parseFloat(priceStr);
    if(limitPrice > 0){
      const r = await fetch('/api/panic', {
        method:'POST',
        body:JSON.stringify({asset:asset, order_type:'LIMIT', price:limitPrice})
      });
      const d = await r.json();
      alert(d.msg);
      update();
    } else {
      alert("يرجى إدخال سعر صحيح");
    }
  }
}

function updateUptime(){
  const diff = Math.floor((Date.now() - startTs) / 1000);
  const hrs = Math.floor(diff / 3600);
  const mins = Math.floor((diff % 3600) / 60);
  const secs = diff % 60;
  document.getElementById('uptime').innerText = `${hrs}س ${mins}د ${secs}ث`;
}
setInterval(updateUptime, 1000);

async function update(){
  try{
    const res = await fetch('/api/data');
    if(res.status===401) location.href='/login';
    const d = await res.json();
    
    if(d.start_timestamp) startTs = d.start_timestamp * 1000;
    
    document.getElementById('live-usdt').innerText = (d.real_balance_usdt || 0.0).toFixed(2) + ' $';
    document.getElementById('live-total-usd').innerText = (d.total_wallet_usd_value || 0.0).toFixed(2) + ' $';
    document.getElementById('api-stat').innerHTML = d.api_connected ? '<span style="color:var(--success)">🟢 MEXC متصل</span>' : '<span style="color:var(--danger)">🔴 API غير متصل</span>';

    // تحديث بيانات وإحصائيات البوتات الثلاثة
    ['BOT_1', 'BOT_2', 'BOT_3'].forEach(bKey => {
      const pfx = bKey === 'BOT_1' ? 'b1' : (bKey === 'BOT_2' ? 'b2' : 'b3');
      const bObj = d.bots[bKey];
      
      const stEl = document.getElementById(pfx+'-st');
      stEl.innerText = bObj.status || 'RUNNING';
      stEl.style.color = bObj.status === 'RUNNING' ? '#10b981' : (bObj.status === 'PAUSED' ? '#f59e0b' : '#ef4444');

      const pnl = bObj.daily_pnl || 0.0;
      const pnlEl = document.getElementById(pfx+'-pnl');
      pnlEl.innerText = (pnl >= 0 ? '+' : '') + pnl.toFixed(3) + '$';
      pnlEl.style.color = pnl >= 0 ? 'var(--success)' : 'var(--danger)';
      
      const pct = Math.min(100, Math.max(0, (pnl / (bObj.daily_target || 5.0)) * 100));
      document.getElementById(pfx+'-pnl-bar').style.width = pct + '%';
      document.getElementById(pfx+'-pnl-pct').innerText = pct.toFixed(0) + '%';

      const totalT = bObj.trades_count || 0;
      const winT = bObj.winning_count || 0;
      const wr = totalT > 0 ? ((winT / totalT) * 100).toFixed(1) : '0.0';
      document.getElementById(pfx+'-winrate').innerText = wr + '%';
      document.getElementById(pfx+'-trade-stats').innerText = `${totalT} صفقة (${winT} رابحة)`;

      let totalOpen = 0;
      let coinsTableHtml = '';
      for(const sym of Object.keys(bObj.active_positions)){
        const count = (bObj.active_positions[sym] || []).length;
        totalOpen += count;
        const coinPnl = (bObj.daily_pnl_coins && bObj.daily_pnl_coins[sym]) ? bObj.daily_pnl_coins[sym] : 0.0;
        const price = d.market_prices[sym] ? d.market_prices[sym].bid : 0.0;

        coinsTableHtml += `<tr>
          <td><strong>${sym}</strong></td>
          <td>${price ? price.toFixed(4)+'$' : '-'}</td>
          <td style="color:${coinPnl>=0?'var(--success)':'var(--danger)'};font-weight:bold">${(coinPnl>=0?'+':'')+coinPnl.toFixed(3)}$</td>
          <td><span class="badge ${count>0?'badge-active':'badge-idle'}">${count}/5 صفقات</span></td>
          <td><button class="btn" style="background:var(--primary);color:#fff;padding:2px 8px" onclick="triggerBuy('${bKey}','${sym}')">⚡ شراء</button></td>
        </tr>`;
      }

      if(document.getElementById(pfx+'-coins-table')){
        document.getElementById(pfx+'-coins-table').querySelector('tbody').innerHTML = coinsTableHtml;
      }
      document.getElementById(pfx+'-open-count').innerText = totalOpen + ' صفقات';
      document.getElementById(pfx+'-cap-used').innerText = (totalOpen * 10) + '$ محجوزة في السوق';

      let posHtml = '';
      for(let s in bObj.active_positions){
        (bObj.active_positions[s] || []).forEach(p=>{
          const highestCol = pfx === 'b3' ? `<td>${p.highest_price||p.entry_price}$</td>` : '';
          posHtml += `<tr><td><strong>${s}</strong></td><td>${p.entry_price}$</td>${highestCol}<td>${p.qty}</td><td>${p.time}</td><td><button class="btn" style="background:var(--danger);color:#fff;padding:2px 6px" onclick="closeSinglePos('${bKey}','${s}', '${p.id}')">🔥 تسييل</button></td></tr>`;
        });
      }
      const colSpan = pfx === 'b3' ? 6 : 5;
      document.getElementById(pfx+'-orders').querySelector('tbody').innerHTML = posHtml || `<tr><td colspan="${colSpan}" style="text-align:center;color:var(--sub)">لا توجد صفقات مفتوحة</td></tr>`;
    });

    let b3MHtml = '';
    for(let sym of Object.keys(d.market_prices)){
      const p = d.market_prices[sym];
      b3MHtml += `<tr><td><strong>${sym}</strong></td><td>${p.ask>0?p.ask+'$':'-'}</td><td><button class="btn" style="background:#0ea5e9;color:#fff;padding:2px 8px" onclick="triggerBuy('BOT_3','${sym}')">🎯 شراء وتسليم الآلي</button></td></tr>`;
    }
    document.getElementById('b3-market-table').querySelector('tbody').innerHTML = b3MHtml;

    // جدول المحفظة مع القيمة الفعلية وخياري التسييل (سوق / أمر ليميت)
    let wHtml = '';
    (d.wallet_assets||[]).forEach(a=>{
      const canSell = a.asset !== 'USDT' && a.free > 0;
      const priceStr = a.asset === 'USDT' ? '1.00 $' : (a.usd_price > 0 ? a.usd_price.toFixed(4) + ' $' : '-');
      const valStr = a.usd_value > 0 ? a.usd_value.toFixed(2) + ' $' : '0.00 $';
      
      wHtml += `<tr>
        <td><strong>${a.asset}</strong></td>
        <td>${a.free}</td>
        <td>${a.locked}</td>
        <td>${priceStr}</td>
        <td style="color:#38bdf8;font-weight:bold">${valStr}</td>
        <td>
          ${canSell ? `
            <div class="action-cell">
              <button class="btn" style="background:var(--danger);color:#fff;padding:2px 6px" onclick="panicMarket('${a.asset}')">🔥 سوق</button>
              <button class="btn" style="background:#f59e0b;color:#000;padding:2px 6px" onclick="panicLimit('${a.asset}', ${a.usd_price})">📝 أمر ليميت</button>
            </div>
          ` : '-'}
        </td>
      </tr>`;
    });
    document.getElementById('w-table').querySelector('tbody').innerHTML = wHtml || '<tr><td colspan="6" style="text-align:center;color:var(--sub)">لا توجد أرصدة ظاهرة</td></tr>';

    let lHtml = '';
    logsText = '';
    (d.recent_logs||[]).forEach(l=>{
      lHtml += `<div style="padding:3px 0;border-bottom:1px solid #1f293d44"><span style="color:var(--sub)">[${l.time}]</span> <span>${l.msg}</span></div>`;
      logsText += `[${l.time}] ${l.msg}\\n`;
    });
    document.getElementById('logs').innerHTML = lHtml;
  }catch(e){}
}
setInterval(update, 2000);
update();
</script>
</body>
</html>"""

# =====================================================================
# 🛡️ خادم الويب ومعالجة الطلبات
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
        elif self.path == '/api/logout':
            self.send_response(200); self.send_header('Set-Cookie', 'session_id=; Path=/; Max-Age=0'); self.end_headers()
        else:
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        data = json.loads(self.rfile.read(length).decode('utf-8')) if length > 0 else {}

        if self.path == '/api/login':
            if database.verify_user(data.get("username", ""), data.get("password", "")):
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

        if self.path == '/api/change_password':
            new_p = data.get("new_password", "").strip()
            if new_p:
                database.change_password(new_p)
                add_log("تم تحديث كلمة مرور لوحة التحكم بنجاح", "info")
                self.send_response(200); self.end_headers()
            else:
                self.send_response(400); self.end_headers()

        elif self.path == '/api/save_keys':
            database.save_keys(data.get("api_key", ""), data.get("api_secret", ""))
            add_log("تم تحديث مفاتيح MEXC في قاعدة البيانات", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/control':
            b_name = data.get("bot_name", "BOT_1")
            st = data.get("status", "RUNNING")
            database.update_bot_config(b_name, {"status": st})
            shared_state["bots"][b_name]["status"] = st
            add_log(f"تم تغيير حالة {b_name} إلى: {st}", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/save_bot_config':
            b_name = data.pop("bot_name", "BOT_1")
            database.update_bot_config(b_name, data)
            add_log(f"تم تحديث إعدادات {b_name} في قاعدة البيانات", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/manual_buy':
            sym = data.get("symbol")
            b_name = data.get("bot_name", "BOT_1")
            cfg = database.get_bot_config(b_name)
            size = float(cfg.get("trade_size_usdt", 10.0))
            bid, ask = get_orderbook(sym)
            if ask:
                q = float(format_quantity(sym, size / ask))
                ok, res = place_order(sym, "BUY", qty=q, quote_qty=size)
                if ok:
                    shared_state["bots"][b_name]["active_positions"][sym].append({
                        'id': f"{b_name.lower()}_{int(time.time()*1000)}", 'entry_price': ask, 'highest_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")
                    })
                    msg = f"✅ تم الشراء الحقيقي لـ {sym} عبر {b_name} عند {ask}$"
                    add_log(msg, "primary")
                else: msg = f"❌ فشل الشراء: {res}"
            else: msg = "فشل قراءة السعر"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/close_position':
            sym = data.get("symbol")
            pos_id = data.get("pos_id")
            b_name = data.get("bot_name", "BOT_1")
            bid, ask = get_orderbook(sym)

            new_positions = []
            found = False
            for p in shared_state["bots"][b_name]["active_positions"].get(sym, []):
                if p.get("id") == pos_id and not found:
                    found = True
                    ok, res = place_order(sym, "SELL", qty=p['qty'])
                    cur_price = bid if bid else p['entry_price']
                    pnl = (cur_price - p['entry_price']) * p['qty']
                    shared_state["bots"][b_name]["daily_pnl"] += pnl
                    shared_state["bots"][b_name]["daily_pnl_coins"][sym] += pnl
                    shared_state["bots"][b_name]["trades_count"] += 1
                    if pnl > 0: shared_state["bots"][b_name]["winning_count"] += 1
                    add_log(f"🔥 تسييل حقيقي لـ {sym} في {b_name} بسعر {cur_price}$ PnL: {pnl:+.3f}$", "danger")
                else:
                    new_positions.append(p)
            shared_state["bots"][b_name]["active_positions"][sym] = new_positions
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم تسييل الصفقة بأمر بيع فوري في المنصة"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/panic':
            asset = data.get("asset")
            order_type = data.get("order_type", "MARKET")
            price = data.get("price")
            free_qty = 0.0
            
            for a in shared_state.get("wallet_assets", []):
                if a["asset"] == asset: free_qty = float(a["free"]); break
                
            if free_qty > 0:
                sym = f"{asset}USDT"
                ok, res = place_order(sym, "SELL", qty=free_qty, order_type=order_type, price=price)
                if ok:
                    msg = f"✅ تم تسييل {asset} بأمر {order_type}" + (f" بسعر {price}$" if price else "")
                    add_log(msg, "danger")
                else:
                    msg = f"❌ فشل تنفيذ الأمر: {res}"
                    add_log(msg, "warning")
            else:
                msg = "لا يوجد رصيد متاح للتسييل"
                
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args): return

if __name__ == "__main__":
    print(f"🚀 بدء تشغيل Command Hub على المنفذ: {PORT}")
    threading.Thread(target=trading_engine_loop, daemon=True).start()
    with socketserver.TCPServer(("", PORT), WebHandler) as srv:
        srv.serve_forever()
