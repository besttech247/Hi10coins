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
# ⚙️ إعدادات التداول والوضع التجريبي (Paper Trading)
# =====================================================================
PAPER_TRADING = True         # True = تداول تجريبي بمحاكاة حية / False = تداول حقيقي بـ API
INITIAL_BALANCE = 500.00     # رصيد المحفظة الافتراضي للبدء ($)

# مفاتيح API (مطلوبة فقط عند تحويل PAPER_TRADING = False)
API_KEY = "YOUR_MEXC_API_KEY"
API_SECRET = "YOUR_MEXC_API_SECRET"
BASE_URL = "https://api.mexc.com"
PORT = 8080

# قائمة الـ 10 عملات المعتمدة
SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

TRADE_SIZE_USDT = 10.0      # حجم كل دخول (10$)
MAX_CONCURRENT_PER_COIN = 5 # أقصى صفقات متزامنة لكل عملة
STOP_LOSS_PCT = 0.0049      # وقف خسارة ثابت -0.49%

# سقف الأهداف اليومية (حجز الأرباح وإعادة التعيين 00:00 UTC)
DAILY_TARGET_PER_COIN = 1.50
DAILY_TARGET_PORTFOLIO = 5.00

ssl_ctx = ssl._create_unverified_context()

# =====================================================================
# 📊 الحالة العامة للمحفظة ولوحة التحكم
# =====================================================================
bot_state = {
    "paper_mode": PAPER_TRADING,
    "virtual_balance": INITIAL_BALANCE,
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "daily_pnl_portfolio": 0.0,
    "total_realized_pnl": 0.0,
    "total_trades_count": 0,
    "winning_trades_count": 0,
    "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
    "active_positions": {sym: [] for sym in SYMBOLS},
    "market_prices": {sym: {"bid": 0.0, "ask": 0.0} for sym in SYMBOLS},
    "recent_logs": []
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    bot_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(bot_state["recent_logs"]) > 50:
        bot_state["recent_logs"].pop()

# =====================================================================
# 📡 دوال جلب الأسعار والشموع المباشرة
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
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as res:
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
# 🔄 محرك التداول (Paper Trading Engine)
# =====================================================================
def trading_loop():
    add_log(f"تم بدء التداول التجريبي المباشر برصيد {INITIAL_BALANCE:.2f}$ على 10 عملات", "success")
    
    while True:
        try:
            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != bot_state["current_day"]:
                add_log(f"🌅 بداية يوم تداول جديد ({now_day} UTC) - تصفير الأهداف اليومية", "info")
                bot_state["current_day"] = now_day
                bot_state["daily_pnl_portfolio"] = 0.0
                bot_state["daily_pnl_coins"] = {sym: 0.0 for sym in SYMBOLS}

            port_target_locked = bot_state["daily_pnl_portfolio"] >= DAILY_TARGET_PORTFOLIO

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

                # 1. إدارة وإغلاق الصفقات المفتوحة
                still_open = []
                for pos in bot_state["active_positions"][sym]:
                    entry = pos['entry_price']
                    qty = pos['qty']
                    sl = entry * (1.0 - STOP_LOSS_PCT)

                    # تحقق ضرب وقف الخسارة
                    if bid <= sl:
                        pnl = (bid - entry) * qty
                        bot_state["virtual_balance"] += (TRADE_SIZE_USDT + pnl)
                        bot_state["daily_pnl_portfolio"] += pnl
                        bot_state["total_realized_pnl"] += pnl
                        bot_state["daily_pnl_coins"][sym] += pnl
                        bot_state["total_trades_count"] += 1
                        add_log(f"🛑 ضرب الوقف لـ {sym} عند {bid}$ (PnL: {pnl:+.3f}$)", "danger")

                    # تحقق جني الأرباح بزخم EWO
                    elif (e2 > 0) and (e1 < e2):
                        pnl = (ask - entry) * qty
                        bot_state["virtual_balance"] += (TRADE_SIZE_USDT + pnl)
                        bot_state["daily_pnl_portfolio"] += pnl
                        bot_state["total_realized_pnl"] += pnl
                        bot_state["daily_pnl_coins"][sym] += pnl
                        bot_state["total_trades_count"] += 1
                        bot_state["winning_trades_count"] += 1
                        add_log(f"🎯 جني أرباح EWO لـ {sym} عند {ask}$ (PnL: {pnl:+.3f}$)", "success")
                    else:
                        still_open.append(pos)

                bot_state["active_positions"][sym] = still_open

                # 2. فحص الدخول في صفقات جديدة
                coin_target_locked = bot_state["daily_pnl_coins"][sym] >= DAILY_TARGET_PER_COIN
                sig_rebound = (e1 < 0) and (e1 > e2) and (e2 <= e3)
                can_open = len(bot_state["active_positions"][sym]) < MAX_CONCURRENT_PER_COIN
                has_balance = bot_state["virtual_balance"] >= TRADE_SIZE_USDT

                if sig_rebound and can_open and has_balance and not port_target_locked and not coin_target_locked:
                    buy_price = bid
                    qty = round(TRADE_SIZE_USDT / buy_price, 4)
                    
                    bot_state["virtual_balance"] -= TRADE_SIZE_USDT
                    bot_state["active_positions"][sym].append({
                        'entry_price': buy_price,
                        'qty': qty,
                        'time': datetime.now(timezone.utc).strftime("%H:%M:%S")
                    })
                    add_log(f"🚀 شراء تجريبي لـ {sym} بسعر {buy_price}$ ({len(bot_state['active_positions'][sym])}/{MAX_CONCURRENT_PER_COIN})", "primary")

        except Exception as e:
            add_log(f"تنبيه محرك التداول: {e}", "warning")
            
        time.sleep(12)

# =====================================================================
# 🌐 واجهة التحكم المباشرة (Dashboard UI)
# =====================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>EWO Bot Dashboard - Live Paper Trading</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f3f4f6;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:14px;line-height:1.5}
.container{max-width:960px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;background:var(--card);border-radius:14px;border:1px solid var(--border);margin-bottom:14px}
.pill{background:#10b98122;color:var(--success);padding:4px 12px;border-radius:20px;font-size:12px;font-weight:bold;display:flex;align-items:center;gap:6px}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--success);display:inline-block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px}
.card-title{font-size:12px;color:var(--sub);margin-bottom:4px}
.card-val{font-size:22px;font-weight:bold}
.progress-bar{height:6px;background:#1f293d;border-radius:3px;margin-top:8px;overflow:hidden}
.progress-fill{height:100%;background:var(--primary);transition:width .4s}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow-x:auto;margin-bottom:14px}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:10px 14px;font-size:13px;border-bottom:1px solid var(--border)}
th{color:var(--sub);font-weight:600}
.badge{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:bold}
.badge-active{background:#10b98122;color:var(--success)}
.badge-idle{background:#64748b22;color:var(--sub)}
.logs{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:12px;max-height:240px;overflow-y:auto;font-family:monospace;font-size:12px}
.log-row{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid #1f293d44}
.log-time{color:var(--sub)}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h2 style="font-size:17px">🤖 EWO Momentum Bot (Paper Trading)</h2>
      <p style="font-size:12px;color:var(--sub)">محاكاة حية على 10 عملات ببيانات السوق اللحظية</p>
    </div>
    <div class="pill">وضع المحاكاة نشط</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-title">رصيد المحفظة الافتراضي</div>
      <div class="card-val" id="balance-val">500.00$</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px">الرصيد الأساسي: 500.00$</div>
    </div>
    <div class="card">
      <div class="card-title">أرباح اليوم المحققة</div>
      <div class="card-val" id="pnl-val">+0.00$</div>
      <div class="progress-bar"><div class="progress-fill" id="pnl-bar" style="width:0%"></div></div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:4px">
        <span>الهدف: 5.00$</span><span id="pnl-pct">0%</span>
      </div>
    </div>
    <div class="card">
      <div class="card-title">نسبة الفوز الإجمالية</div>
      <div class="card-val" id="win-rate">0.0%</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="trade-stats">0 صفقات منفذة</div>
    </div>
    <div class="card">
      <div class="card-title">الصفقات والسيولة المفتوحة</div>
      <div class="card-val" id="open-count">0</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px" id="cap-used">0$ محجوزة في السوق</div>
    </div>
  </div>

  <h3 style="font-size:14px;margin-bottom:8px">📊 حالة العملات العشر والصفقات الجارية</h3>
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>العملة</th><th>سعر السوق</th><th>ربح اليوم</th><th>الصفقات المفتوحة</th></tr>
      </thead>
      <tbody id="coin-rows"></tbody>
    </table>
  </div>

  <h3 style="font-size:14px;margin-bottom:8px">📜 سجل العمليات الفورية (Live Events)</h3>
  <div class="logs" id="log-box"></div>
</div>

<script>
async function refreshDashboard(){
  try{
    const res = await fetch('/api/data');
    const d = await res.json();
    
    // الرصيد
    document.getElementById('balance-val').innerText = d.virtual_balance.toFixed(2) + '$';
    
    // الأرباح والهدف اليومي
    const pnl = d.daily_pnl_portfolio;
    const pnlEl = document.getElementById('pnl-val');
    pnlEl.innerText = (pnl>=0?'+':'') + pnl.toFixed(3) + '$';
    pnlEl.style.color = pnl>=0?'var(--success)':'var(--danger)';
    
    const pct = Math.min(100, Math.max(0, (pnl / 5.0) * 100));
    document.getElementById('pnl-bar').style.width = pct + '%';
    document.getElementById('pnl-pct').innerText = pct.toFixed(0) + '%';
    
    // نسبة الفوز
    const totalT = d.total_trades_count;
    const winT = d.winning_trades_count;
    const wr = totalT > 0 ? ((winT / totalT) * 100).toFixed(1) : '0.0';
    document.getElementById('win-rate').innerText = wr + '%';
    document.getElementById('trade-stats').innerText = `${totalT} صفقة (${winT} رابحة)`;
    
    // جدول العملات
    let totalOpen = 0;
    let rowsHtml = '';
    
    for(const sym of Object.keys(d.active_positions)){
      const count = d.active_positions[sym].length;
      totalOpen += count;
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
    document.getElementById('open-count').innerText = totalOpen + ' صفقات';
    document.getElementById('cap-used').innerText = (totalOpen * 10) + '$ محجوزة في السوق';
    
    // السجلات
    let logHtml = '';
    for(const l of d.recent_logs){
      logHtml += `<div class="log-row"><span class="log-time">[${l.time}]</span><span>${l.msg}</span></div>`;
    }
    document.getElementById('log-box').innerHTML = logHtml || '<div style="color:var(--sub)">في انتظار أول إشارة دخول...</div>';
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

    def log_message(self, format, *args):
        return

def run_web_server():
    with socketserver.TCPServer(("", PORT), DashboardServer) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    print("="*70, flush=True)
    print("🚀 تم تشغيل محاكي التداول التجريبي (Paper Trading) بنجاح!", flush=True)
    print(f"💰 الرصيد التجريبي: {INITIAL_BALANCE:.2f}$ | عدد العملات: {len(SYMBOLS)}", flush=True)
    print(f"🌐 افتح الواجهة من متصفح هاتفك عبر الرابط: http://127.0.0.1:{PORT}", flush=True)
    print("="*70, flush=True)

    # تشغيل خادم الواجهة في خيط منفصل
    t_server = threading.Thread(target=run_web_server, daemon=True)
    t_server.start()

    # تشغيل محرك التداول التجريبي
    trading_loop()
