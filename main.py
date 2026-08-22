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

SYMBOL_RULES = {}

shared_state = {
    "api_connected": False,
    "has_saved_keys": False,
    "masked_key": "",
    "server_public_ip": "جاري الجلب...",
    "real_balance_usdt": 0.0,
    "total_wallet_usd_value": 0.0,
    "wallet_assets": [],
    "market_prices": {},
    "recent_logs": [],
    "start_timestamp": START_TIME,
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "bots": {
        "BOT_1": {
            "name": "🤖 Bot 1 (EWO 5m)",
            "status": "RUNNING",
            "symbols": [],
            "max_allocation": 100.0,
            "max_concurrent": 3,
            "daily_pnl": 0.0,
            "daily_target": 5.0,
            "daily_coin_target": 1.5,
            "trades_count": 0,
            "winning_count": 0,
            "daily_pnl_coins": {},
            "active_positions": {}
        },
        "BOT_2": {
            "name": "⚡ Bot 2 (EWO Custom TF)",
            "status": "PAUSED",
            "symbols": [],
            "max_allocation": 100.0,
            "max_concurrent": 3,
            "daily_pnl": 0.0,
            "daily_target": 5.0,
            "daily_coin_target": 1.5,
            "trades_count": 0,
            "winning_count": 0,
            "daily_pnl_coins": {},
            "active_positions": {}
        },
        "BOT_3": {
            "name": "🎯 Bot 3 (Manual Trigger + Auto Bracket)",
            "status": "RUNNING",
            "symbols": [],
            "max_allocation": 100.0,
            "max_concurrent": 3,
            "daily_pnl": 0.0,
            "daily_target": 5.0,
            "daily_coin_target": 1.5,
            "trades_count": 0,
            "winning_count": 0,
            "daily_pnl_coins": {},
            "active_positions": {}
        }
    }
}

def add_log(msg, category="system", log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    shared_state["recent_logs"].insert(0, {
        "time": timestamp,
        "msg": msg,
        "cat": category,
        "type": log_type
    })
    if len(shared_state["recent_logs"]) > 250:
        shared_state["recent_logs"].pop()

def fetch_server_ip():
    try:
        req = urllib.request.Request("https://api.ipify.org?format=json", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
            d = json.loads(res.read().decode('utf-8'))
            shared_state["server_public_ip"] = d.get("ip", "غير متوفر")
    except Exception:
        shared_state["server_public_ip"] = "تعذر تحديد IP"

def sanitize_str(val):
    if not val: return ""
    return str(val).strip().replace("\r", "").replace("\n", "").replace("\t", "").replace(" ", "")

def parse_symbols_list(sym_str):
    if not sym_str: return []
    items = [s.strip().upper() for s in sym_str.split(",") if s.strip()]
    cleaned = []
    for s in items:
        if not s.endswith("USDT") and not s.endswith("USDC"):
            s = f"{s}USDT"
        cleaned.append(s)
    return list(dict.fromkeys(cleaned))

def sign_query(query_string, secret):
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def mexc_private_request(endpoint, method="GET", params=None):
    keys = database.get_keys()
    api_key = sanitize_str(keys.get("api_key", ""))
    api_secret = sanitize_str(keys.get("api_secret", ""))

    if not api_key or not api_secret:
        shared_state["has_saved_keys"] = False
        shared_state["masked_key"] = ""
        return False, "مفاتيح API مفقودة"

    shared_state["has_saved_keys"] = True
    shared_state["masked_key"] = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "****"

    if params is None: params = {}
    params["recvWindow"] = 60000
    params["timestamp"] = int(time.time() * 1000)

    query_string = urllib.parse.urlencode(params)
    signature = sign_query(query_string, api_secret)
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {"X-MEXC-APIKEY": api_key, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as res:
            return True, json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            err_json = json.loads(err_body)
            return False, f"[{err_json.get('code')}] {err_json.get('msg')}"
        except Exception:
            return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)

def update_exchange_info(symbol):
    if symbol in SYMBOL_RULES: return
    try:
        url = f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=4) as res:
            d = json.loads(res.read().decode('utf-8'))
            for s in d.get("symbols", []):
                if s["symbol"] == symbol:
                    base_prec = int(s.get("baseAssetPrecision", 2))
                    quote_prec = int(s.get("quotePrecision", 4))
                    SYMBOL_RULES[symbol] = {"base_prec": base_prec, "quote_prec": quote_prec}
                    return
    except Exception:
        pass
    SYMBOL_RULES[symbol] = {"base_prec": 2, "quote_prec": 4}

def format_quantity(symbol, qty):
    update_exchange_info(symbol)
    prec = SYMBOL_RULES.get(symbol, {}).get("base_prec", 2)
    factor = 10 ** prec
    truncated = math.floor(qty * factor) / factor
    return f"{int(truncated)}" if prec == 0 else f"{truncated:.{prec}f}"

def format_price(symbol, price):
    update_exchange_info(symbol)
    prec = SYMBOL_RULES.get(symbol, {}).get("quote_prec", 4)
    return f"{price:.{prec}f}"

def get_orderbook(symbol):
    try:
        url = f"{BASE_URL}/api/v3/ticker/bookTicker?symbol={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=4) as res:
            d = json.loads(res.read().decode('utf-8'))
            return float(d['bidPrice']), float(d['askPrice'])
    except Exception:
        return None, None

def fetch_klines(symbol, interval="5m", limit=45):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=6) as res:
            data = json.loads(res.read().decode('utf-8'))
            return [{'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4])} for r in data]
    except Exception:
        return []

def place_order(symbol, side, qty=None, quote_qty=None, order_type="MARKET", price=None):
    params = {"symbol": symbol, "side": side.upper(), "type": order_type.upper()}
    if order_type.upper() == "LIMIT":
        if not price or not qty: return False, "السعر والكمية مطلوبة"
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
    
    add_log(f"📤 إرسال طلب {side.upper()} لـ {symbol} ({order_type})", "orders", "info")
    ok, res = mexc_private_request("/api/v3/order", method="POST", params=params)
    if not ok:
        add_log(f"❌ خطأ أمر {symbol}: {res}", "orders", "danger")
    return ok, res

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

# =====================================================================
# 🤖 محرك التداول المركزي
# =====================================================================
def trading_engine_loop():
    fetch_server_ip()
    time.sleep(2)
    add_log(f"محرك التداول نشط (IP: {shared_state['server_public_ip']})", "system", "info")
    while True:
        try:
            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != shared_state["current_day"]:
                shared_state["current_day"] = now_day
                for bKey in ["BOT_1", "BOT_2", "BOT_3"]:
                    shared_state["bots"][bKey]["daily_pnl"] = 0.0
                    for s in shared_state["bots"][bKey]["daily_pnl_coins"]:
                        shared_state["bots"][bKey]["daily_pnl_coins"][s] = 0.0
                add_log(f"🌅 تصفير الأهداف اليومية ({now_day} UTC)", "system", "info")

            configs = {
                "BOT_1": database.get_bot_config("BOT_1"),
                "BOT_2": database.get_bot_config("BOT_2"),
                "BOT_3": database.get_bot_config("BOT_3")
            }

            all_active_symbols = set()
            for bKey, cfg in configs.items():
                syms = parse_symbols_list(cfg.get("symbols", ""))
                shared_state["bots"][bKey]["symbols"] = syms
                shared_state["bots"][bKey]["status"] = cfg.get("status", "RUNNING")
                shared_state["bots"][bKey]["max_allocation"] = float(cfg.get("max_allocation_usdt", 100.0))
                shared_state["bots"][bKey]["max_concurrent"] = int(cfg.get("max_concurrent_per_coin", 3))
                
                for s in syms:
                    all_active_symbols.add(s)
                    if s not in shared_state["bots"][bKey]["active_positions"]:
                        shared_state["bots"][bKey]["active_positions"][s] = []
                    if s not in shared_state["bots"][bKey]["daily_pnl_coins"]:
                        shared_state["bots"][bKey]["daily_pnl_coins"][s] = 0.0

            for sym in all_active_symbols:
                bid, ask = get_orderbook(sym)
                if bid and ask:
                    shared_state["market_prices"][sym] = {"bid": bid, "ask": ask}

            ok, acc = mexc_private_request("/api/v3/account")
            if ok and isinstance(acc, dict) and "balances" in acc:
                shared_state["api_connected"] = True
                assets = []
                usdt_free = 0.0
                total_val_usd = 0.0
                for b in acc["balances"]:
                    free = float(b["free"])
                    locked = float(b["locked"])
                    total = free + locked
                    asset = b["asset"]
                    if total > 0.0:
                        usd_price = 1.0 if asset == "USDT" else shared_state["market_prices"].get(f"{asset}USDT", {}).get("bid", 0.0)
                        val_usd = total * usd_price
                        total_val_usd += val_usd
                        assets.append({"asset": asset, "free": free, "locked": locked, "total": total, "usd_price": usd_price, "usd_value": val_usd})
                    if asset == "USDT": usdt_free = free
                shared_state["wallet_assets"] = assets
                shared_state["real_balance_usdt"] = usdt_free
                shared_state["total_wallet_usd_value"] = total_val_usd
            else:
                shared_state["api_connected"] = False

            for bKey in ["BOT_1", "BOT_2", "BOT_3"]:
                cfg = configs[bKey]
                if cfg.get("status") == "STOPPED": continue

                sym_list = shared_state["bots"][bKey]["symbols"]
                size = float(cfg.get("trade_size_usdt", 10.0))
                max_alloc = shared_state["bots"][bKey]["max_allocation"]
                max_con = shared_state["bots"][bKey]["max_concurrent"]
                
                total_open_trades = sum(len(shared_state["bots"][bKey]["active_positions"].get(s, [])) for s in sym_list)
                current_used_cap = total_open_trades * size

                for sym in sym_list:
                    p_info = shared_state["market_prices"].get(sym)
                    if not p_info or not p_info["bid"]: continue
                    bid = p_info["bid"]
                    ask = p_info["ask"]

                    # Bot 1 & Bot 2
                    if bKey in ["BOT_1", "BOT_2"]:
                        tf = "5m" if bKey == "BOT_1" else cfg.get("timeframe", "15m")
                        candles = fetch_klines(sym, interval=tf, limit=45)
                        if candles:
                            e3, e2, e1 = calculate_ewo(candles)
                            if e1 is not None:
                                sl_pct = float(cfg.get("sl_pct", 0.005))
                                still_pos = []
                                for pos in shared_state["bots"][bKey]["active_positions"].get(sym, []):
                                    sl = pos['entry_price'] * (1.0 - sl_pct)
                                    hit_sl = bid <= sl
                                    hit_rev = (e2 > 0) and (e1 < e2)
                                    if hit_sl or hit_rev:
                                        reason = "🛑 وقف الخسارة SL" if hit_sl else "🎯 انعكاس EWO"
                                        ok, res = place_order(sym, "SELL", qty=pos['qty'])
                                        if ok:
                                            pnl = (bid - pos['entry_price']) * pos['qty']
                                            shared_state["bots"][bKey]["daily_pnl"] += pnl
                                            shared_state["bots"][bKey]["daily_pnl_coins"][sym] += pnl
                                            shared_state["bots"][bKey]["trades_count"] += 1
                                            if pnl > 0: shared_state["bots"][bKey]["winning_count"] += 1
                                            add_log(f"💰 [{bKey}] بيع {sym} | PnL: {pnl:+.3f}$ ({reason})", "sells", "success" if pnl > 0 else "danger")
                                        else: still_pos.append(pos)
                                    else: still_pos.append(pos)
                                shared_state["bots"][bKey]["active_positions"][sym] = still_pos

                                sig_rebound = (e1 < 0 and e1 > e2 and e2 <= e3)
                                can_open_coin = len(still_pos) < max_con
                                can_open_alloc = (current_used_cap + size) <= max_alloc
                                port_not_locked = shared_state["bots"][bKey]["daily_pnl"] < shared_state["bots"][bKey]["daily_target"]

                                if cfg.get("status") == "RUNNING" and sig_rebound and can_open_coin and can_open_alloc and port_not_locked:
                                    if shared_state["real_balance_usdt"] >= size:
                                        q = float(format_quantity(sym, size / ask))
                                        if q > 0:
                                            ok, res = place_order(sym, "BUY", qty=q, quote_qty=size)
                                            if ok:
                                                shared_state["bots"][bKey]["active_positions"][sym].append({
                                                    'id': f"{bKey.lower()}_{int(time.time()*1000)}", 'entry_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")
                                                })
                                                current_used_cap += size
                                                add_log(f"🚀 [{bKey}] شراء {sym} عند {ask}$ ({len(still_pos)+1}/{max_con})", "buys", "primary")

                    # Bot 3
                    elif bKey == "BOT_3":
                        tp_pct = float(cfg.get("tp_pct", 0.015))
                        sl_pct = float(cfg.get("sl_pct", 0.005))
                        use_ts = bool(cfg.get("trailing_stop", 1))
                        cb_pct = float(cfg.get("trailing_cb", 0.003))

                        still_b3 = []
                        for pos in shared_state["bots"]["BOT_3"]["active_positions"].get(sym, []):
                            entry = pos['entry_price']
                            highest = pos.get('highest_price', entry)
                            if bid > highest:
                                highest = bid
                                pos['highest_price'] = highest

                            tp_price = entry * (1.0 + tp_pct)
                            sl_price = entry * (1.0 - sl_pct)
                            trailing_sl = highest * (1.0 - cb_pct) if use_ts else sl_price
                            effective_sl = max(sl_price, trailing_sl) if use_ts and highest >= (entry * (1.0 + cb_pct)) else sl_price

                            hit_tp = bid >= tp_price
                            hit_sl = bid <= effective_sl

                            if hit_tp or hit_sl:
                                reason = "🎯 TP" if hit_tp else ("🔄 TS" if use_ts and effective_sl > sl_price else "🛑 SL")
                                ok, res = place_order(sym, "SELL", qty=pos['qty'])
                                if ok:
                                    pnl = (bid - entry) * pos['qty']
                                    shared_state["bots"]["BOT_3"]["daily_pnl"] += pnl
                                    shared_state["bots"]["BOT_3"]["daily_pnl_coins"][sym] += pnl
                                    shared_state["bots"]["BOT_3"]["trades_count"] += 1
                                    if pnl > 0: shared_state["bots"]["BOT_3"]["winning_count"] += 1
                                    add_log(f"💰 [Bot 3] إغلاق {sym} | PnL: {pnl:+.3f}$ ({reason})", "sells", "success" if pnl > 0 else "danger")
                                else: still_b3.append(pos)
                            else: still_b3.append(pos)
                        shared_state["bots"]["BOT_3"]["active_positions"][sym] = still_b3

        except Exception as e:
            add_log(f"خطأ محرك التداول: {e}", "system", "warning")

        time.sleep(7)

# =====================================================================
# 🌐 واجهات HTML
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

ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>التحليلات المتقدمة</title>
<style>
body{background:#090d16;color:#f3f4f6;font-family:system-ui;padding:16px}
.card{background:#111827;border:1px solid #1f293d;border-radius:12px;padding:16px;margin-bottom:12px}
.btn{padding:6px 12px;background:#3b82f6;color:#fff;border:none;border-radius:6px;font-weight:bold;text-decoration:none;display:inline-block}
</style>
</head>
<body>
  <div class="card" style="display:flex;justify-content:space-between;align-items:center">
    <h2>📊 صفحة التحليلات المتقدمة والتقارير</h2>
    <a href="/" class="btn">🔙 العودة للرئيسية</a>
  </div>
  <div class="card">
    <p style="color:#94a3b8">هذه الصفحة مستقلة وجاهزة للبرمجة وربط الرسوم البيانية والتقارير المتقدمة لاحقاً.</p>
  </div>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>MEXC Multi-Bot Hub</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f3f4f6;--sub:#94a3b8}
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:12px;line-height:1.5}
.header-box{display:flex;justify-content:space-between;align-items:center;padding:12px;background:var(--card);border-radius:12px;border:1px solid var(--border);margin-bottom:12px;flex-wrap:wrap;gap:10px}
.wallet-bar{display:flex;gap:12px;align-items:center;background:#151e30;padding:6px 12px;border-radius:8px;border:1px solid var(--border)}
.tabs{display:flex;gap:6px;margin:10px 0;overflow-x:auto}
.tab{padding:8px 12px;background:#151e30;border:1px solid var(--border);border-radius:8px;color:var(--sub);cursor:pointer;font-weight:bold;white-space:nowrap}
.tab.active{background:var(--primary);color:#fff}
.tab-pane{display:none}
.tab-pane.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-bottom:10px}
.card-title{font-size:11px;color:var(--sub);margin-bottom:2px}
.card-val{font-size:20px;font-weight:bold}
.progress-bar{height:5px;background:#1f293d;border-radius:3px;margin-top:6px;overflow:hidden}
.progress-fill{height:100%;background:var(--primary);transition:width .4s}
.btn{padding:5px 8px;border:none;border-radius:6px;font-weight:bold;cursor:pointer;font-size:12px;display:inline-flex;align-items:center;justify-content:center;gap:4px}
.icon-btn{padding:4px 8px;font-size:14px;border-radius:6px;border:none;cursor:pointer}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:7px 8px;border-bottom:1px solid var(--border);font-size:12px}
th{color:var(--sub)}
.badge{padding:2px 6px;border-radius:4px;font-size:11px;font-weight:bold}
.badge-active{background:#10b98122;color:var(--success)}
.badge-idle{background:#64748b22;color:var(--sub)}
.logs{max-height:200px;overflow-y:auto;font-family:monospace;font-size:11px;background:#090d16;padding:8px;border-radius:8px;border:1px solid var(--border)}
.log-item{padding:3px 0;border-bottom:1px solid #1f293d44;display:flex;gap:6px}
input,select{background:#090d16;border:1px solid var(--border);color:#fff;padding:6px 8px;border-radius:6px;font-size:12px;width:100%}
details{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;overflow:hidden}
summary{padding:8px 12px;cursor:pointer;font-weight:bold;background:#151e30}
.form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;padding:10px}
</style>
</head>
<body>
  <div class="header-box">
    <div>
      <div style="display:flex;align-items:center;gap:8px">
        <strong>🎛️ MEXC Multi-Bot Hub</strong>
        <a href="/analytics" style="color:#60a5fa;font-size:12px;text-decoration:none;background:#1e293b;padding:2px 8px;border-radius:6px">📊 التحليلات ↗</a>
      </div>
      <div style="font-size:11px;color:var(--sub);margin-top:2px">⏳ <span id="uptime" style="color:#60a5fa;font-weight:bold">00:00:00</span></div>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <div class="wallet-bar">
        <div><span style="font-size:11px;color:var(--sub)">USDT:</span> <strong id="live-usdt" style="color:#10b981;font-size:14px">0.00 $</strong></div>
        <div style="border-right:1px solid var(--border);padding-right:8px"><span style="font-size:11px;color:var(--sub)">الإجمالي:</span> <strong id="live-total-usd" style="color:#38bdf8;font-size:14px">0.00 $</strong></div>
      </div>
      <span id="api-stat" style="font-size:12px">جاري الفحص...</span>
      <button class="icon-btn" style="background:#334155;color:#fff" title="تسجيل الخروج" onclick="fetch('/api/logout').then(()=>location.href='/login')">🚪</button>
    </div>
  </div>

  <details id="keys-box">
    <summary style="color:#60a5fa">🔑 الإعدادات و IP السيرفر ▾ <span id="keys-status-badge"></span></summary>
    <div style="padding:10px">
      <div style="background:#090d16;padding:6px 10px;border-radius:6px;border:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:11px;color:var(--sub)">🌐 IP السيرفر: <strong id="server-ip-val" style="color:#38bdf8;font-family:monospace">جاري الجلب...</strong></span>
        <button class="btn" style="background:#0284c7;color:#fff;font-size:11px" onclick="copyServerIP()">📋 نسخ IP</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px">
        <input type="password" id="m-key" placeholder="MEXC API Key">
        <input type="password" id="m-sec" placeholder="MEXC API Secret">
      </div>
      <button class="btn" style="background:var(--primary);color:#fff;width:100%;margin-bottom:8px" onclick="saveKeys()">💾 حفظ المفاتيح</button>
      <div style="display:grid;grid-template-columns:1fr auto;gap:6px">
        <input type="password" id="new-pass" placeholder="كلمة المرور الجديدة">
        <button class="btn" style="background:#10b981;color:#fff" onclick="changePass()">تحديث 🔒</button>
      </div>
    </div>
  </details>

  <div class="tabs">
    <button class="tab active" onclick="showTab('t1', this)">🤖 Bot 1</button>
    <button class="tab" onclick="showTab('t2', this)">⚡ Bot 2</button>
    <button class="tab" onclick="showTab('t3', this)">🎯 Bot 3</button>
    <button class="tab" onclick="showTab('t4', this)">💰 المحفظة</button>
  </div>

  <!-- Bot 1 -->
  <div id="t1" class="tab-pane active">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>حالة Bot 1: <strong id="b1-st" style="color:#10b981">RUNNING</strong></span>
      <div style="display:flex;gap:4px">
        <button class="icon-btn" style="background:var(--success);color:#fff" title="تشغيل" onclick="setSt('BOT_1','RUNNING')">▶️</button>
        <button class="icon-btn" style="background:#f59e0b;color:#000" title="إيقاف مؤقت (شراء فقط)" onclick="setSt('BOT_1','PAUSED')">⏸️</button>
        <button class="icon-btn" style="background:var(--danger);color:#fff" title="إيقاف تام" onclick="setSt('BOT_1','STOPPED')">⏹️</button>
      </div>
    </div>
    
    <div class="grid">
      <div class="card">
        <div class="card-title">أرباح اليوم</div>
        <div class="card-val" id="b1-pnl">+0.00$</div>
        <div class="progress-bar"><div class="progress-fill" id="b1-pnl-bar" style="width:0%"></div></div>
      </div>
      <div class="card">
        <div class="card-title">نسبة الفوز</div>
        <div class="card-val" id="b1-winrate">0.0%</div>
        <div style="font-size:10px;color:var(--sub)" id="b1-trade-stats">0 صفقات</div>
      </div>
      <div class="card">
        <div class="card-title">السيولة المفتوحة</div>
        <div class="card-val" id="b1-cap-used">0$</div>
        <div style="font-size:10px;color:var(--sub)" id="b1-alloc-text">سقف: 100$</div>
      </div>
    </div>

    <details>
      <summary style="color:#a78bfa">⚙️ سقف رأس المال والصفقات (Bot 1) ▾</summary>
      <div class="form-row">
        <div><label style="font-size:11px;color:var(--sub)">سقف رأس المال ($)</label><input type="number" id="b1-alloc" value="100"></div>
        <div><label style="font-size:11px;color:var(--sub)">أقصى صفقات/عملة</label><input type="number" id="b1-maxcon" value="3"></div>
        <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="b1-size" value="10"></div>
        <div><label style="font-size:11px;color:var(--sub)">وقف الخسارة (SL %)</label><input type="number" id="b1-sl" value="0.49" step="0.01"></div>
        <div style="display:flex;align-items:flex-end"><button class="btn" style="background:var(--primary);color:#fff;width:100%" onclick="saveBotCfg('BOT_1', 'b1')">💾 حفظ الإعدادات</button></div>
      </div>
    </details>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <strong style="font-size:12px">📊 جدول العملات وأرباح اليوم:</strong>
        <button class="btn" style="background:#10b981;color:#fff;font-size:11px;padding:3px 8px" onclick="addCoinToBot('BOT_1')">➕ إضافة عملة</button>
      </div>
      <div style="overflow-x:auto"><table id="b1-coins-table"><thead><tr><th>العملة</th><th>السعر</th><th>ربح اليوم</th><th>الصفقات</th><th>دخول</th><th>حذف</th></tr></thead><tbody></tbody></table></div>
    </div>

    <div class="card">
      <strong style="font-size:12px;display:block;margin-bottom:4px">📂 الصفقات المفتوحة:</strong>
      <div style="overflow-x:auto"><table id="b1-orders"><thead><tr><th>العملة</th><th>الدخول</th><th>الكمية</th><th>الوقت</th><th>تسييل</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Bot 2 -->
  <div id="t2" class="tab-pane">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>حالة Bot 2: <strong id="b2-st" style="color:#f59e0b">PAUSED</strong></span>
      <div style="display:flex;gap:4px">
        <button class="icon-btn" style="background:var(--success);color:#fff" title="تشغيل" onclick="setSt('BOT_2','RUNNING')">▶️</button>
        <button class="icon-btn" style="background:#f59e0b;color:#000" title="إيقاف مؤقت" onclick="setSt('BOT_2','PAUSED')">⏸️</button>
        <button class="icon-btn" style="background:var(--danger);color:#fff" title="إيقاف تام" onclick="setSt('BOT_2','STOPPED')">⏹️</button>
      </div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-title">أرباح اليوم</div>
        <div class="card-val" id="b2-pnl">+0.00$</div>
        <div class="progress-bar"><div class="progress-fill" id="b2-pnl-bar" style="width:0%"></div></div>
      </div>
      <div class="card">
        <div class="card-title">نسبة الفوز</div>
        <div class="card-val" id="b2-winrate">0.0%</div>
        <div style="font-size:10px;color:var(--sub)" id="b2-trade-stats">0 صفقات</div>
      </div>
      <div class="card">
        <div class="card-title">السيولة المفتوحة</div>
        <div class="card-val" id="b2-cap-used">0$</div>
        <div style="font-size:10px;color:var(--sub)" id="b2-alloc-text">سقف: 100$</div>
      </div>
    </div>

    <details>
      <summary style="color:#a78bfa">⚙️ الفريم وسقف رأس المال (Bot 2) ▾</summary>
      <div class="form-row">
        <div>
          <label style="font-size:11px;color:var(--sub)">الفريم الزمني</label>
          <select id="b2-tf">
            <option value="1m">1m</option><option value="5m">5m</option><option value="15m" selected>15m</option><option value="30m">30m</option><option value="60m">1h</option><option value="4h">4h</option>
          </select>
        </div>
        <div><label style="font-size:11px;color:var(--sub)">سقف رأس المال ($)</label><input type="number" id="b2-alloc" value="100"></div>
        <div><label style="font-size:11px;color:var(--sub)">أقصى صفقات/عملة</label><input type="number" id="b2-maxcon" value="3"></div>
        <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="b2-size" value="10"></div>
        <div><label style="font-size:11px;color:var(--sub)">وقف الخسارة (SL %)</label><input type="number" id="b2-sl" value="0.6" step="0.01"></div>
        <div style="display:flex;align-items:flex-end"><button class="btn" style="background:var(--primary);color:#fff;width:100%" onclick="saveBotCfg('BOT_2', 'b2')">💾 حفظ</button></div>
      </div>
    </details>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <strong style="font-size:12px">📊 جدول العملات وأرباح اليوم:</strong>
        <button class="btn" style="background:#10b981;color:#fff;font-size:11px;padding:3px 8px" onclick="addCoinToBot('BOT_2')">➕ إضافة عملة</button>
      </div>
      <div style="overflow-x:auto"><table id="b2-coins-table"><thead><tr><th>العملة</th><th>السعر</th><th>ربح اليوم</th><th>الصفقات</th><th>دخول</th><th>حذف</th></tr></thead><tbody></tbody></table></div>
    </div>

    <div class="card">
      <strong style="font-size:12px;display:block;margin-bottom:4px">📂 الصفقات المفتوحة:</strong>
      <div style="overflow-x:auto"><table id="b2-orders"><thead><tr><th>العملة</th><th>الدخول</th><th>الكمية</th><th>الوقت</th><th>تسييل</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Bot 3 -->
  <div id="t3" class="tab-pane">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>حالة Bot 3: <strong id="b3-st" style="color:#10b981">RUNNING</strong></span>
      <div style="display:flex;gap:4px">
        <button class="icon-btn" style="background:var(--success);color:#fff" title="تشغيل" onclick="setSt('BOT_3','RUNNING')">▶️</button>
        <button class="icon-btn" style="background:#f59e0b;color:#000" title="إيقاف مؤقت" onclick="setSt('BOT_3','PAUSED')">⏸️</button>
      </div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-title">أرباح اليوم</div>
        <div class="card-val" id="b3-pnl">+0.00$</div>
        <div class="progress-bar"><div class="progress-fill" id="b3-pnl-bar" style="width:0%"></div></div>
      </div>
      <div class="card">
        <div class="card-title">نسبة الفوز</div>
        <div class="card-val" id="b3-winrate">0.0%</div>
        <div style="font-size:10px;color:var(--sub)" id="b3-trade-stats">0 صفقات</div>
      </div>
      <div class="card">
        <div class="card-title">السيولة المفتوحة</div>
        <div class="card-val" id="b3-cap-used">0$</div>
        <div style="font-size:10px;color:var(--sub)" id="b3-alloc-text">سقف: 100$</div>
      </div>
    </div>

    <details open>
      <summary style="color:#38bdf8">🎯 إعدادات الدخول اليدوي والـ Trailing Stop ▾</summary>
      <div class="form-row">
        <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="b3-size" value="10"></div>
        <div><label style="font-size:11px;color:var(--sub)">جني الأرباح (TP %)</label><input type="number" id="b3-tp" value="1.5" step="0.1"></div>
        <div><label style="font-size:11px;color:var(--sub)">وقف الخسارة (SL %)</label><input type="number" id="b3-sl" value="0.5" step="0.1"></div>
        <div>
          <label style="font-size:11px;color:var(--sub)">Trailing Stop</label>
          <select id="b3-ts"><option value="1">مفعّل ✅</option><option value="0">معطّل ❌</option></select>
        </div>
        <div style="display:flex;align-items:flex-end"><button class="btn" style="background:var(--primary);color:#fff;width:100%" onclick="saveBotCfg('BOT_3', 'b3')">💾 حفظ</button></div>
      </div>
    </details>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <strong style="font-size:12px">⚡ عملات التداول السريع:</strong>
        <button class="btn" style="background:#10b981;color:#fff;font-size:11px;padding:3px 8px" onclick="addCoinToBot('BOT_3')">➕ إضافة عملة</button>
      </div>
      <div style="overflow-x:auto"><table id="b3-market-table"><thead><tr><th>العملة</th><th>السعر</th><th>إطلاق بنقرة</th><th>حذف</th></tr></thead><tbody></tbody></table></div>
    </div>

    <div class="card">
      <strong style="font-size:12px;display:block;margin-bottom:4px">📂 صفقات Bot 3 المفتوحة:</strong>
      <div style="overflow-x:auto"><table id="b3-orders"><thead><tr><th>العملة</th><th>الدخول</th><th>أعلى سعر</th><th>الكمية</th><th>الوقت</th><th>تسييل</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Wallet -->
  <div id="t4" class="tab-pane">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:6px">
        <strong style="font-size:12px">💼 أرصدة المحفظة:</strong>
        <div style="display:flex;gap:4px">
          <button class="btn" style="background:#8b5cf6;color:#fff;font-size:11px" onclick="convertDust()">🔄 تحويل لـ MX</button>
          <button class="btn" style="background:var(--danger);color:#fff;font-size:11px" onclick="panicSellAll()">🔥 تسييل الكل لـ USDT</button>
        </div>
      </div>
      <div style="overflow-x:auto">
        <table id="w-table">
          <thead><tr><th>العملة</th><th>المتاح</th><th>المحجوز</th><th>السعر</th><th>القيمة ($)</th><th>تسييل</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Logs -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;flex-wrap:wrap;gap:6px">
      <strong style="font-size:12px">📜 السجل المباشر:</strong>
      <div style="display:flex;gap:4px">
        <input type="text" id="log-search" placeholder="🔍 بحث..." style="width:120px;padding:2px 6px;font-size:11px" oninput="renderLogs()">
        <button class="icon-btn" style="background:#334155;color:#fff" title="نسخ السجلات" onclick="copyLogs()">📋</button>
      </div>
    </div>
    <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px">
      <button class="btn" style="background:#151e30;color:var(--sub);font-size:10px" onclick="setLogFilter('all', this)">الكل</button>
      <button class="btn" style="background:#151e30;color:var(--sub);font-size:10px" onclick="setLogFilter('signals', this)">🎯 إشارات</button>
      <button class="btn" style="background:#151e30;color:var(--sub);font-size:10px" onclick="setLogFilter('buys', this)">🚀 شراء</button>
      <button class="btn" style="background:#151e30;color:var(--sub);font-size:10px" onclick="setLogFilter('sells', this)">💰 بيع</button>
      <button class="btn" style="background:#151e30;color:var(--sub);font-size:10px" onclick="setLogFilter('orders', this)">📤 أوامر</button>
    </div>
    <div class="logs" id="logs"></div>
  </div>

<script>
let rawLogs = [];
let logsText = "";
let currentLogFilter = "all";
let startTs = Date.now();
let keysLoaded = false;
let currentPublicIP = "";

function showTab(id, btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
function setLogFilter(cat, btn){
  currentLogFilter = cat;
  renderLogs();
}
function copyLogs(){
  navigator.clipboard.writeText(logsText).then(()=>alert("✅ تم نسخ السجلات!"));
}
function copyServerIP(){
  if(currentPublicIP && currentPublicIP !== "جاري الجلب..."){
    navigator.clipboard.writeText(currentPublicIP).then(()=>alert("✅ تم نسخ IP السيرفر: " + currentPublicIP));
  }
}
async function setSt(b,s){
  await fetch('/api/control',{method:'POST',body:JSON.stringify({bot_name:b,status:s})});
  update();
}
async function saveKeys(){
  let k = document.getElementById('m-key').value.replace(/\\s+/g, '');
  let s = document.getElementById('m-sec').value.replace(/\\s+/g, '');
  if(!k || !s){ alert("يرجى إدخال المفتاح والسر"); return; }
  await fetch('/api/save_keys',{method:'POST',body:JSON.stringify({api_key:k, api_secret:s})});
  alert('✅ تم حفظ مفاتيح MEXC!');
  update();
}
async function changePass(){
  const p = document.getElementById('new-pass').value;
  if(!p){ alert("أدخل كلمة المرور"); return; }
  const res = await fetch('/api/change_password', {method:'POST', body:JSON.stringify({new_password:p})});
  if(res.ok){ alert("✅ تم التغيير!"); document.getElementById('new-pass').value = ''; }
}

// دالة إضافة عملة جديدة للبوت
async function addCoinToBot(botName){
  const coin = prompt(`أدخل رمز العملة المراد إضافتها لـ ${botName} (مثال: SOL أو SUI أو NEAR):`);
  if(coin && coin.trim()){
    const r = await fetch('/api/add_symbol', {
      method: 'POST',
      body: JSON.stringify({bot_name: botName, symbol: coin.trim().toUpperCase()})
    });
    const d = await r.json();
    alert(d.msg);
    update();
  }
}

// دالة حذف عملة مع رسالة تأكيد
async function removeCoinFromBot(botName, sym){
  if(confirm(`⚠️ هل أنت متأكد من حذف العملة (${sym}) من قائمة تداول ${botName}؟`)){
    const r = await fetch('/api/remove_symbol', {
      method: 'POST',
      body: JSON.stringify({bot_name: botName, symbol: sym})
    });
    const d = await r.json();
    alert(d.msg);
    update();
  }
}

async function saveBotCfg(botName, prefix){
  const payload = {
    bot_name: botName,
    max_allocation_usdt: parseFloat(document.getElementById(prefix+'-alloc').value)||100,
    max_concurrent_per_coin: parseInt(document.getElementById(prefix+'-maxcon').value)||3,
    trade_size_usdt: parseFloat(document.getElementById(prefix+'-size').value)||10
  };
  if(prefix==='b1' || prefix==='b2'){
    payload.sl_pct = (parseFloat(document.getElementById(prefix+'-sl').value)||0.5) / 100.0;
  }
  if(prefix==='b2'){
    payload.timeframe = document.getElementById('b2-tf').value;
  }
  if(prefix==='b3'){
    payload.tp_pct = (parseFloat(document.getElementById(prefix+'-tp').value)||1.5) / 100.0;
    payload.sl_pct = (parseFloat(document.getElementById(prefix+'-sl').value)||0.5) / 100.0;
    payload.trailing_stop = parseInt(document.getElementById('b3-ts').value);
  }
  await fetch('/api/save_bot_config', {method:'POST', body:JSON.stringify(payload)});
  alert(`✅ تم حفظ إعدادات ${botName}!`);
  update();
}
async function triggerBuy(botName, sym){
  if(confirm(`إطلاق شراء لـ ${sym} عبر ${botName}؟`)){
    const r = await fetch('/api/manual_buy', {method:'POST', body:JSON.stringify({bot_name:botName, symbol:sym})});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}
async function closeSinglePos(botName, sym, posId){
  if(confirm(`تسييل صفقة ${sym} فوري؟`)){
    const r = await fetch('/api/close_position', {method:'POST', body:JSON.stringify({bot_name:botName, symbol:sym, pos_id:posId})});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}
async function panicMarket(asset){
  if(confirm(`تسييل ${asset} بسعر السوق؟`)){
    const r = await fetch('/api/panic',{method:'POST',body:JSON.stringify({asset:asset, order_type:'MARKET'})});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}
async function panicLimit(asset, curPrice){
  const priceStr = prompt(`سعر أمر LIMIT لـ ${asset}:`, curPrice);
  if(priceStr){
    const limitPrice = parseFloat(priceStr);
    if(limitPrice > 0){
      const r = await fetch('/api/panic', {method:'POST', body:JSON.stringify({asset:asset, order_type:'LIMIT', price:limitPrice})});
      const d = await r.json();
      alert(d.msg);
      update();
    }
  }
}
async function convertDust(){
  if(confirm("تحويل الأرصدة الصغيرة لـ MX؟")){
    const r = await fetch('/api/convert_dust', {method:'POST'});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}
async function panicSellAll(){
  if(confirm("⚠️ تسييل كل العملات لـ USDT؟")){
    const r = await fetch('/api/panic_all', {method:'POST'});
    const d = await r.json();
    alert(d.msg);
    update();
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

function renderLogs(){
  const searchQ = (document.getElementById('log-search').value || '').toLowerCase();
  let lHtml = '';
  logsText = '';

  rawLogs.forEach(l => {
    const cat = l.cat || 'system';
    const msgLower = l.msg.toLowerCase();
    if(currentLogFilter !== 'all' && cat !== currentLogFilter) return;
    if(searchQ && !msgLower.includes(searchQ) && !cat.includes(searchQ)) return;

    let typeColor = '#f3f4f6';
    if(l.type === 'danger') typeColor = 'var(--danger)';
    else if(l.type === 'success') typeColor = 'var(--success)';
    else if(l.type === 'warning') typeColor = '#f59e0b';
    else if(l.type === 'primary') typeColor = '#60a5fa';

    lHtml += `<div class="log-item"><span style="color:var(--sub)">[${l.time}]</span> <span style="color:${typeColor}">${l.msg}</span></div>`;
    logsText += `[${l.time}] ${l.msg}\\n`;
  });
  document.getElementById('logs').innerHTML = lHtml || '<div style="color:var(--sub);text-align:center">لا توجد أحداث</div>';
}

async function update(){
  try{
    const res = await fetch('/api/data');
    if(res.status===401) location.href='/login';
    const d = await res.json();
    
    if(d.start_timestamp) startTs = d.start_timestamp * 1000;
    if(d.server_public_ip){
      currentPublicIP = d.server_public_ip;
      document.getElementById('server-ip-val').innerText = d.server_public_ip;
    }

    document.getElementById('live-usdt').innerText = (d.real_balance_usdt || 0.0).toFixed(2) + ' $';
    document.getElementById('live-total-usd').innerText = (d.total_wallet_usd_value || 0.0).toFixed(2) + ' $';
    document.getElementById('api-stat').innerHTML = d.api_connected ? '<span style="color:var(--success)">🟢 متصل</span>' : '<span style="color:var(--danger)">🔴 غير متصل</span>';

    if(d.has_saved_keys){
      document.getElementById('keys-status-badge').innerHTML = `<span style="color:var(--success);font-weight:bold">(${d.masked_key})</span>`;
      if(!keysLoaded){
        document.getElementById('m-key').placeholder = `محفوظ (${d.masked_key})`;
        document.getElementById('m-sec').placeholder = "محفوظ (*********)";
        keysLoaded = true;
      }
    }

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

      const totalT = bObj.trades_count || 0;
      const winT = bObj.winning_count || 0;
      const wr = totalT > 0 ? ((winT / totalT) * 100).toFixed(1) : '0.0';
      document.getElementById(pfx+'-winrate').innerText = wr + '%';
      document.getElementById(pfx+'-trade-stats').innerText = `${totalT} صفقات (${winT} رابحة)`;

      // جدول العملات الخاص بالبوت مع زر الحذف المنفصل
      let totalOpen = 0;
      let coinsTableHtml = '';
      (bObj.symbols || []).forEach(sym => {
        const count = (bObj.active_positions[sym] || []).length;
        totalOpen += count;
        const coinPnl = (bObj.daily_pnl_coins && bObj.daily_pnl_coins[sym]) ? bObj.daily_pnl_coins[sym] : 0.0;
        const price = d.market_prices[sym] ? d.market_prices[sym].bid : 0.0;

        coinsTableHtml += `<tr>
          <td><strong>${sym}</strong></td>
          <td>${price ? price.toFixed(4)+'$' : '-'}</td>
          <td style="color:${coinPnl>=0?'var(--success)':'var(--danger)'};font-weight:bold">${(coinPnl>=0?'+':'')+coinPnl.toFixed(3)}$</td>
          <td><span class="badge ${count>0?'badge-active':'badge-idle'}">${count}/${bObj.max_concurrent}</span></td>
          <td><button class="icon-btn" style="background:var(--primary);color:#fff" title="شراء فوري" onclick="triggerBuy('${bKey}','${sym}')">⚡</button></td>
          <td><button class="icon-btn" style="background:#334155;color:#f87171" title="حذف من الجدول" onclick="removeCoinFromBot('${bKey}','${sym}')">🗑️</button></td>
        </tr>`;
      });

      if(document.getElementById(pfx+'-coins-table')){
        document.getElementById(pfx+'-coins-table').querySelector('tbody').innerHTML = coinsTableHtml || '<tr><td colspan="6" style="text-align:center">لا توجد عملات مضافة حالياً</td></tr>';
      }
      
      const usedUsd = totalOpen * (parseFloat(document.getElementById(pfx+'-size').value)||10);
      document.getElementById(pfx+'-cap-used').innerText = usedUsd.toFixed(1) + '$';
      document.getElementById(pfx+'-alloc-text').innerText = `سقف: ${bObj.max_allocation}$`;

      // الصفقات المفتوحة
      let posHtml = '';
      for(let s in bObj.active_positions){
        (bObj.active_positions[s] || []).forEach(p=>{
          const highestCol = pfx === 'b3' ? `<td>${p.highest_price||p.entry_price}$</td>` : '';
          posHtml += `<tr><td><strong>${s}</strong></td><td>${p.entry_price}$</td>${highestCol}<td>${p.qty}</td><td>${p.time}</td><td><button class="icon-btn" style="background:var(--danger);color:#fff" title="تسييل" onclick="closeSinglePos('${bKey}','${s}', '${p.id}')">🔥</button></td></tr>`;
        });
      }
      const colSpan = pfx === 'b3' ? 6 : 5;
      document.getElementById(pfx+'-orders').querySelector('tbody').innerHTML = posHtml || `<tr><td colspan="${colSpan}" style="text-align:center;color:var(--sub)">لا توجد صفقات</td></tr>`;
    });

    // Bot 3 Market Trigger Table
    let b3MHtml = '';
    (d.bots["BOT_3"].symbols || []).forEach(sym => {
      const p = d.market_prices[sym];
      const ask = p ? p.ask : 0.0;
      b3MHtml += `<tr>
        <td><strong>${sym}</strong></td>
        <td>${ask>0?ask+'$':'-'}</td>
        <td><button class="btn" style="background:#0ea5e9;color:#fff;font-size:11px" onclick="triggerBuy('BOT_3','${sym}')">🎯 شراء وتسليم</button></td>
        <td><button class="icon-btn" style="background:#334155;color:#f87171" title="حذف من القائمة" onclick="removeCoinFromBot('BOT_3','${sym}')">🗑️</button></td>
      </tr>`;
    });
    document.getElementById('b3-market-table').querySelector('tbody').innerHTML = b3MHtml || '<tr><td colspan="4" style="text-align:center">لا توجد عملات مضافة</td></tr>';

    // جدول المحفظة
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
            <div style="display:flex;gap:3px">
              <button class="icon-btn" style="background:var(--danger);color:#fff" title="تسييل سوق" onclick="panicMarket('${a.asset}')">🔥</button>
              <button class="icon-btn" style="background:#f59e0b;color:#000" title="تسييل ليميت" onclick="panicLimit('${a.asset}', ${a.usd_price})">📝</button>
            </div>
          ` : '-'}
        </td>
      </tr>`;
    });
    document.getElementById('w-table').querySelector('tbody').innerHTML = wHtml || '<tr><td colspan="6" style="text-align:center">لا توجد أرصدة</td></tr>';

    rawLogs = d.recent_logs || [];
    renderLogs();
  }catch(e){}
}
setInterval(update, 2000);
update();
</script>
</body>
</html>"""

# =====================================================================
# 🛡️ خادم الويب ومعالجة مسارات إضافة/حذف العملات
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
        elif self.path == '/analytics':
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            self.wfile.write(ANALYTICS_HTML.encode('utf-8'))
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
                add_log("تم تحديث كلمة المرور", "system", "info")
                self.send_response(200); self.end_headers()
            else:
                self.send_response(400); self.end_headers()

        elif self.path == '/api/save_keys':
            api_k = sanitize_str(data.get("api_key", ""))
            api_s = sanitize_str(data.get("api_secret", ""))
            database.save_keys(api_k, api_s)
            ok, acc = mexc_private_request("/api/v3/account")
            if ok:
                shared_state["api_connected"] = True
                add_log("✅ تم تأكيد اتصال مفاتيح MEXC", "system", "success")
            else:
                add_log(f"⚠️ فشل التحقق من المفاتيح: {acc}", "system", "warning")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/control':
            b_name = data.get("bot_name", "BOT_1")
            st = data.get("status", "RUNNING")
            database.update_bot_config(b_name, {"status": st})
            shared_state["bots"][b_name]["status"] = st
            add_log(f"تغيير حالة {b_name} إلى: {st}", "system", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/save_bot_config':
            b_name = data.pop("bot_name", "BOT_1")
            database.update_bot_config(b_name, data)
            add_log(f"تم حفظ إعدادات {b_name}", "system", "info")
            self.send_response(200); self.end_headers()

        # إضافة عملة للبوت
        elif self.path == '/api/add_symbol':
            b_name = data.get("bot_name", "BOT_1")
            raw_sym = data.get("symbol", "").strip().upper()
            if raw_sym:
                if not raw_sym.endswith("USDT") and not raw_sym.endswith("USDC"):
                    raw_sym = f"{raw_sym}USDT"
                
                cfg = database.get_bot_config(b_name)
                current_syms = parse_symbols_list(cfg.get("symbols", ""))
                if raw_sym not in current_syms:
                    current_syms.append(raw_sym)
                    database.update_bot_config(b_name, {"symbols": ", ".join(current_syms)})
                    add_log(f"➕ إضافة {raw_sym} إلى قائمة {b_name}", "system", "success")
                    msg = f"✅ تمت إضافة {raw_sym} بنجاح!"
                else:
                    msg = "العملة موجودة بالفعل في القائمة"
            else:
                msg = "رمز العملة غير صالح"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        # حذف عملة من البوت
        elif self.path == '/api/remove_symbol':
            b_name = data.get("bot_name", "BOT_1")
            sym = data.get("symbol", "").strip().upper()
            cfg = database.get_bot_config(b_name)
            current_syms = parse_symbols_list(cfg.get("symbols", ""))
            if sym in current_syms:
                current_syms.remove(sym)
                database.update_bot_config(b_name, {"symbols": ", ".join(current_syms)})
                add_log(f"🗑️ إزالة {sym} من قائمة {b_name}", "system", "warning")
                msg = f"✅ تم حذف {sym} من {b_name}"
            else:
                msg = "العملة غير موجودة"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

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
                    if sym not in shared_state["bots"][b_name]["active_positions"]:
                        shared_state["bots"][b_name]["active_positions"][sym] = []
                    shared_state["bots"][b_name]["active_positions"][sym].append({
                        'id': f"{b_name.lower()}_{int(time.time()*1000)}", 'entry_price': ask, 'highest_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")
                    })
                    msg = f"✅ تم شراء {sym} عبر {b_name} عند {ask}$"
                    add_log(msg, "buys", "primary")
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
                    add_log(f"🔥 تسييل {sym} في {b_name} بسعر {cur_price}$ | PnL: {pnl:+.3f}$", "sells", "danger")
                else:
                    new_positions.append(p)
            shared_state["bots"][b_name]["active_positions"][sym] = new_positions
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم تسييل الصفقة"}, ensure_ascii=False).encode('utf-8'))

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
                    add_log(msg, "sells", "danger")
                else:
                    msg = f"❌ فشل تنفيذ الأمر: {res}"
            else:
                msg = "لا يوجد رصيد متاح للتسييل"

            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/convert_dust':
            ok, res = mexc_private_request("/api/v3/capital/convert", method="POST")
            msg = "✅ تم تحويل الأرصدة الصغيرة لـ MX" if ok else f"❌ لم يتم التحويل: {res}"
            add_log(msg, "system", "info")
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/panic_all':
            sold_count = 0
            for a in shared_state.get("wallet_assets", []):
                asset = a["asset"]
                free_qty = float(a["free"])
                if asset != "USDT" and free_qty > 0:
                    sym = f"{asset}USDT"
                    ok, res = place_order(sym, "SELL", qty=free_qty, order_type="MARKET")
                    if ok:
                        sold_count += 1
                        add_log(f"🔥 تسييل {asset} بنجاح", "sells", "danger")

            msg = f"✅ تم تسييل {sold_count} عملات إلى USDT" if sold_count > 0 else "لا توجد عملات متاحة"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args): return

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

if __name__ == "__main__":
    print(f"🚀 بدء تشغيل Command Hub على 0.0.0.0:{PORT}", flush=True)
    t_engine = threading.Thread(target=trading_engine_loop, daemon=True)
    t_engine.start()

    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, WebHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
