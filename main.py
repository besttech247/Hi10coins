import http.server
import socketserver
import threading
import json
import urllib.request
import urllib.parse
import ssl
import time
import hmac
import hashlib
from datetime import datetime, timezone

# =====================================================================
# ⚙️ الإعدادات العامة الافتراضية
# =====================================================================
CONFIG = {
    "paper_trading": True,
    "api_key": "",
    "api_secret": "",
    "trade_size_usdt": 10.0,
    "initial_capital": 500.0,
    "max_concurrent_per_coin": 5,
    "stop_loss_pct": 0.0049,
    "daily_target_per_coin": 1.50,
    "daily_target_portfolio": 5.00
}

BASE_URL = "https://api.mexc.com"
PORT = 8080

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

ssl_ctx = ssl._create_unverified_context()

# =====================================================================
# 📊 الحالة العامة للمحفظة ولوحة التحكم
# =====================================================================
bot_state = {
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
    "recent_logs": []
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    bot_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(bot_state["recent_logs"]) > 60:
        bot_state["recent_logs"].pop()

# =====================================================================
# 🔐 دوال الربط وتنفيذ الأوامر مع MEXC API
# =====================================================================
def sign_query(query_string, secret):
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def mexc_private_request(endpoint, method="GET", params=None):
    if not CONFIG["api_key"] or not CONFIG["api_secret"]:
        return None
    if params is None:
        params = {}
    
    params["timestamp"] = int(time.time() * 1000)
    query_string = urllib.parse.urlencode(params)
    signature = sign_query(query_string, CONFIG["api_secret"])
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    
    headers = {
        "X-MEXC-APIKEY": CONFIG["api_key"],
        "Content-Type": "application/json"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=7) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        add_log(f"خطأ API ({e.code}): {err_msg}", "danger")
        return None
    except Exception as e:
        add_log(f"فشل الاتصال بـ MEXC: {str(e)}", "danger")
        return None

def fetch_real_balance():
    data = mexc_private_request("/api/v3/account", method="GET")
    if data and "balances" in data:
        bot_state["api_connected"] = True
        for asset in data["balances"]:
            if asset["asset"] == "USDT":
                bot_state["real_balance"] = float(asset["free"])
                return float(asset["free"])
    bot_state["api_connected"] = False
    return 0.0

def place_order(symbol, side, qty, price=None):
    if CONFIG["paper_trading"]:
        return {"status": "FILLED", "orderId": f"PAPER_{int(time.time()*1000)}"}
    
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET" if price is None else "LIMIT",
        "quantity": f"{qty:.4f}"
    }
    if price:
        params["price"] = f"{price:.4f}"
        params["timeInForce"] = "GTC"
        
    return mexc_private_request("/api/v3/order", method="POST", params=params)

# =====================================================================
# 📡 دوال جلب الأسعار والشموع
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
# 🔄 محرك التداول (Execution Engine)
# =====================================================================
def trading_loop():
    add_log(f"تم بدء محرك التداول بنجاح", "info")
    
    while True:
        try:
            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != bot_state["current_day"]:
                add_log(f"🌅 بداية يوم جديد ({now_day} UTC) - تصفير الأهداف اليومية", "info")
                bot_state["current_day"] = now_day
                bot_state["daily_pnl_portfolio"] = 0.0
                bot_state["daily_pnl_coins"] = {sym: 0.0 for sym in SYMBOLS}

            if not CONFIG["paper_trading"] and CONFIG["api_key"] and CONFIG["api_secret"]:
                fetch_real_balance()

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

                # 1. إدارة الصفقات المفتوحة
                still_open = []
                for pos in bot_state["active_positions"][sym]:
                    entry = pos['entry_price']
                    qty = pos['qty']
                    sl = entry * (1.0 - CONFIG["stop_loss_pct"])

                    # إغلاق بوقف الخسارة
                    if bid <= sl:
                        order = place_order(sym, "SELL", qty)
                        if order:
                            pnl = (bid - entry) * qty
                            bot_state["virtual_balance"] += (CONFIG["trade_size_usdt"] + pnl)
                            bot_state["daily_pnl_portfolio"] += pnl
                            bot_state["total_realized_pnl"] += pnl
                            bot_state["daily_pnl_coins"][sym] += pnl
                            bot_state["total_trades_count"] += 1
                            add_log(f"🛑 ضرب الوقف لـ {sym} عند {bid}$ (PnL: {pnl:+.3f}$)", "danger")
                        else:
                            still_open.append(pos)

                    # إغلاق بجني الأرباح (EWO)
                    elif (e2 > 0) and (e1 < e2):
                        order = place_order(sym, "SELL", qty)
                        if order:
                            pnl = (ask - entry) * qty
                            bot_state["virtual_balance"] += (CONFIG["trade_size_usdt"] + pnl)
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

                # 2. التحقق من فرص الشراء
                coin_target_locked = bot_state["daily_pnl_coins"][sym] >= CONFIG["daily_target_per_coin"]
                sig_rebound = (e1 < 0) and (e1 > e2) and (e2 <= e3)
                can_open = len(bot_state["active_positions"][sym]) < CONFIG["max_concurrent_per_coin"]
                
                avail_balance = bot_state["virtual_balance"] if CONFIG["paper_trading"] else bot_state["real_balance"]
                has_balance = avail_balance >= CONFIG["trade_size_usdt"]

                if sig_rebound and can_open and has_balance and not port_target_locked and not coin_target_locked:
                    buy_price = ask
                    qty = round(CONFIG["trade_size_usdt"] / buy_price, 4)
                    
                    order = place_order(sym, "BUY", qty)
                    if order:
                        if CONFIG["paper_trading"]:
                            bot_state["virtual_balance"] -= CONFIG["trade_size_usdt"]
                        
                        bot_state["active_positions"][sym].append({
                            'entry_price': buy_price,
                            'qty': qty,
                            'time': datetime.now(timezone.utc).strftime("%H:%M:%S")
                        })
                        mode_tag = "تجريبي" if CONFIG["paper_trading"] else "حقيقي"
                        add_log(f"🚀 شراء ({mode_tag}) لـ {sym} عند {buy_price}$", "primary")

        except Exception as e:
            add_log(f"تنبيه المحرك: {str(e)}", "warning")

        time.sleep(10)

# =====================================================================
# 🌐 واجهة التحكم والتكوين المباشر
# =====================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MEXC EWO Bot Control Center</title>
<style>
:root{--bg:#0b0f19;--card:#121b2d;--border:#1e2d4a;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f8fafc;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:14px;line-height:1.5}
.container{max-width:980px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;background:var(--card);border-radius:12px;border:1px solid var(--border);margin-bottom:14px}
.pill{background:#10b98122;color:var(--success);padding:4px 12px;border-radius:20px;font-size:12px;font-weight:bold}
.pill-danger{background:#ef444422;color:var(--danger)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px}
.card-title{font-size:12px;color:var(--sub);margin-bottom:4px}
.card-val{font-size:20px;font-weight:bold}
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-top:10px}
input, select{background:#090d16;border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;width:100%;font-size:13px}
button{background:var(--primary);color:#fff;border:none;padding:9px 16px;border-radius:8px;font-weight:bold;cursor:pointer;width:100%;margin-top:10px}
button:hover{opacity:0.9}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow-x:auto;margin-bottom:14px}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:10px 14px;font-size:13px;border-bottom:1px solid var(--border)}
th{color:var(--sub)}
.badge{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:bold}
.badge-active{background:#10b98122;color:var(--success)}
.badge-idle{background:#64748b22;color:var(--sub)}
.logs{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px;max-height:220px;overflow-y:auto;font-family:monospace;font-size:12px}
.log-row{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid #1e2d4a55}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h2 style="font-size:17px">🤖 MEXC EWO Auto-Trader</h2>
      <p style="font-size:12px;color:var(--sub)">نظام التداول الآلي وإدارة الصفقات</p>
    </div>
    <div id="mode-badge" class="pill">وضع المحاكاة نشط</div>
  </div>

  <div class="card" style="margin-bottom:14px;">
    <h3 style="font-size:14px;color:var(--primary)">🔑 إعدادات الربط ورأس المال (MEXC API)</h3>
    <form id="config-form">
      <div class="form-grid">
        <div>
          <label style="font-size:11px;color:var(--sub)">نمط التداول</label>
          <select id="cfg-mode">
            <option value="paper">محاكاة (Paper Trading)</option>
            <option value="live">تداول حقيقي (Live MEXC API)</option>
          </select>
        </div>
        <div>
          <label style="font-size:11px;color:var(--sub)">رأس المال المخصص ($)</label>
          <input type="number" id="cfg-capital" placeholder="500.00" step="10">
        </div>
        <div>
          <label style="font-size:11px;color:var(--sub)">حجم الدخول للصفقة الواحدة ($)</label>
          <input type="number" id="cfg-trade-size" placeholder="10.00" step="1">
        </div>
      </div>
      <div class="form-grid" style="margin-top:8px;">
        <div>
          <label style="font-size:11px;color:var(--sub)">MEXC API Key</label>
          <input type="password" id="cfg-key" placeholder="أدخل API Key">
        </div>
        <div>
          <label style="font-size:11px;color:var(--sub)">MEXC API Secret</label>
          <input type="password" id="cfg-secret" placeholder="أدخل API Secret">
        </div>
      </div>
      <button type="button" onclick="saveSettings()">💾 حفظ وتحديث الإعدادات</button>
    </form>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-title">الرصيد الفعلي / المحاكى</div>
      <div class="card-val" id="balance-val">0.00$</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="balance-sub">المحفظة الافتراضية</div>
    </div>
    <div class="card">
      <div class="card-title">أرباح اليوم المحققة</div>
      <div class="card-val" id="pnl-val">+0.00$</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="pnl-sub">الهدف: 5.00$</div>
    </div>
    <div class="card">
      <div class="card-title">نسبة الفوز الإجمالية</div>
      <div class="card-val" id="win-rate">0.0%</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="trade-stats">0 صفقات</div>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>العملة</th><th>سعر السوق</th><th>ربح اليوم</th><th>الصفقات المفتوحة</th></tr>
      </thead>
      <tbody id="coin-rows"></tbody>
    </table>
  </div>

  <h3 style="font-size:13px;margin-bottom:6px;color:var(--sub)">📜 سجل الأحداث المباشر</h3>
  <div class="logs" id="log-box"></div>
</div>

<script>
async function saveSettings(){
  const payload = {
    paper_trading: document.getElementById('cfg-mode').value === 'paper',
    capital: parseFloat(document.getElementById('cfg-capital').value) || 500,
    trade_size: parseFloat(document.getElementById('cfg-trade-size').value) || 10,
    api_key: document.getElementById('cfg-key').value,
    api_secret: document.getElementById('cfg-secret').value
  };
  await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  alert('تم حفظ الإعدادات بنجاح!');
}

async function refreshDashboard(){
  try{
    const res = await fetch('/api/data');
    const d = await res.json();
    
    const badge = document.getElementById('mode-badge');
    if(d.paper_mode){
      badge.className = 'pill';
      badge.innerText = 'وضع المحاكاة نشط';
      document.getElementById('balance-val').innerText = d.virtual_balance.toFixed(2) + '$';
      document.getElementById('balance-sub').innerText = 'رصيد المحاكاة الافتراضي';
    } else {
      badge.className = d.api_connected ? 'pill' : 'pill pill-danger';
      badge.innerText = d.api_connected ? 'اتصال API حقيقي متصل' : 'تنبيه: API غير متصل';
      document.getElementById('balance-val').innerText = d.real_balance.toFixed(2) + '$';
      document.getElementById('balance-sub').innerText = 'رصيد USDT الفعلي في MEXC';
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
    for(const sym of Object.keys(d.active_positions)){
      const count = d.active_positions[sym].length;
      const coinPnl = d.daily_pnl_coins[sym] || 0;
      const price = d.market_prices[sym] ? d.market_prices[sym].bid : 0;
      rowsHtml += `<tr>
        <td><strong>${sym}</strong></td>
        <td>${price ? price.toFixed(4)+'$' : '-'}</td>
        <td style="color:${coinPnl>=0?'var(--success)':'var(--danger)'};font-weight:bold">${(coinPnl>=0?'+':'')+coinPnl.toFixed(3)}$</td>
        <td><span class="badge ${count>0?'badge-active':'badge-idle'}">${count}/5 صفقات</span></td>
      </tr>`;
    }
    document.getElementById('coin-rows').innerHTML = rowsHtml;

    let logHtml = '';
    for(const l of d.recent_logs){
      logHtml += `<div class="log-row"><span style="color:var(--sub)">[${l.time}]</span><span>${l.msg}</span></div>`;
    }
    document.getElementById('log-box').innerHTML = logHtml;
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
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))

    def do_POST(self):
        if self.path == '/api/config':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            cfg = json.loads(post_data.decode('utf-8'))
            
            CONFIG["paper_trading"] = cfg.get("paper_trading", True)
            CONFIG["initial_capital"] = cfg.get("capital", 500.0)
            CONFIG["trade_size_usdt"] = cfg.get("trade_size", 10.0)
            if cfg.get("api_key"):
                CONFIG["api_key"] = cfg.get("api_key")
            if cfg.get("api_secret"):
                CONFIG["api_secret"] = cfg.get("api_secret")
                
            bot_state["paper_mode"] = CONFIG["paper_trading"]
            if CONFIG["paper_trading"]:
                bot_state["virtual_balance"] = CONFIG["initial_capital"]
            
            add_log("تم تحديث الإعدادات ورأس المال من لوحة التحكم", "info")
            self.send_response(200)
            self.end_headers()

    def log_message(self, format, *args):
        return

def run_web_server():
    with socketserver.TCPServer(("", PORT), DashboardServer) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    print("="*65)
    print(f"🚀 خادم البوت يعمل على: http://127.0.0.1:{PORT}")
    print("="*65)
    
    t_server = threading.Thread(target=run_web_server, daemon=True)
    t_server.start()
    
    trading_loop()
