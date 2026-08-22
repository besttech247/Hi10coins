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

# تهيئة المنفذ المتوافق مع Railway وقاعدة البيانات
PORT = int(os.environ.get("PORT", 8080))
database.init_db()

BASE_URL = "https://api.mexc.com"
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

shared_state = {
    "api_connected": False,
    "real_balance": 0.0,
    "wallet_assets": [],
    "market_prices": {sym: {"bid": 0.0, "ask": 0.0} for sym in SYMBOLS},
    "recent_logs": [],
    "bots": {
        "BOT_1": {
            "name": "🤖 EWO Momentum",
            "virtual_balance": 500.0,
            "daily_pnl": 0.0,
            "trades_count": 0,
            "winning_count": 0,
            "active_positions": {sym: [] for sym in SYMBOLS}
        },
        "BOT_2": {
            "name": "⚡ Bot 2 (جاهز للربط)",
            "virtual_balance": 500.0,
            "daily_pnl": 0.0,
            "trades_count": 0,
            "winning_count": 0,
            "active_positions": {sym: [] for sym in SYMBOLS}
        },
        "BOT_3": {
            "name": "📈 Bot 3 (جاهز للربط)",
            "virtual_balance": 500.0,
            "daily_pnl": 0.0,
            "trades_count": 0,
            "winning_count": 0,
            "active_positions": {sym: [] for sym in SYMBOLS}
        }
    }
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    shared_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(shared_state["recent_logs"]) > 100:
        shared_state["recent_logs"].pop()

# =====================================================================
# 🔐 محرك MEXC API
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

def place_order(symbol, side, qty=None, quote_qty=None, is_paper=True):
    if is_paper:
        return True, {"status": "FILLED"}
    params = {"symbol": symbol, "side": side.upper(), "type": "MARKET"}
    if side.upper() == "BUY" and quote_qty:
        params["quoteOrderQty"] = f"{quote_qty:.2f}"
    elif qty:
        params["quantity"] = format_quantity(symbol, qty)
    else:
        return False, "تحديد الكمية مطلوب"
    return mexc_private_request("/api/v3/order", method="POST", params=params)

# =====================================================================
# 🤖 محرك التداول للبوتات
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
    add_log("تم بدء تشغيل محرك التداول (3 بوتات)", "info")
    while True:
        try:
            # مزامنة المحفظة
            ok, acc = mexc_private_request("/api/v3/account")
            if ok and "balances" in acc:
                shared_state["api_connected"] = True
                shared_state["wallet_assets"] = [b for b in acc["balances"] if float(b["free"]) + float(b["locked"]) > 0.0001]
                for b in shared_state["wallet_assets"]:
                    if b["asset"] == "USDT":
                        shared_state["real_balance"] = float(b["free"])
            else:
                shared_state["api_connected"] = False

            cfg_b1 = database.get_bot_config("BOT_1")
            cfg_b2 = database.get_bot_config("BOT_2")
            cfg_b3 = database.get_bot_config("BOT_3")

            for sym in SYMBOLS:
                bid, ask = get_orderbook(sym)
                if bid and ask:
                    shared_state["market_prices"][sym] = {"bid": bid, "ask": ask}

                candles = fetch_klines(sym)
                if not candles or not bid:
                    continue

                # --- تنفيذ البوت الأول: EWO Bot ---
                if cfg_b1.get("status") != "STOPPED":
                    e3, e2, e1 = calculate_ewo(candles)
                    if e1 is not None:
                        is_p = bool(cfg_b1.get("paper_trading", 1))
                        size = float(cfg_b1.get("trade_size_usdt", 10.0))
                        
                        # إدارة الصفقات المفتوحة
                        still = []
                        for pos in shared_state["bots"]["BOT_1"]["active_positions"].get(sym, []):
                            sl = pos['entry_price'] * (1.0 - float(cfg_b1.get("stop_loss_pct", 0.0049)))
                            if bid <= sl or ((e2 > 0) and (e1 < e2)):
                                ok, res = place_order(sym, "SELL", qty=pos['qty'], is_paper=is_p)
                                if ok:
                                    pnl = (bid - pos['entry_price']) * pos['qty']
                                    shared_state["bots"]["BOT_1"]["virtual_balance"] += (size + pnl)
                                    shared_state["bots"]["BOT_1"]["daily_pnl"] += pnl
                                    shared_state["bots"]["BOT_1"]["trades_count"] += 1
                                    if pnl > 0: shared_state["bots"]["BOT_1"]["winning_count"] += 1
                                    add_log(f"[BOT 1] بيع {sym} PnL: {pnl:+.3f}$", "success" if pnl>0 else "danger")
                                else:
                                    still.append(pos)
                            else:
                                still.append(pos)
                        shared_state["bots"]["BOT_1"]["active_positions"][sym] = still

                        # إشارة الدخول
                        if cfg_b1.get("status") == "RUNNING" and len(still) < 5 and (e1 < 0 and e1 > e2 and e2 <= e3):
                            q = float(format_quantity(sym, size / ask))
                            if q > 0:
                                ok, res = place_order(sym, "BUY", qty=q, quote_qty=size, is_paper=is_p)
                                if ok:
                                    if is_p: shared_state["bots"]["BOT_1"]["virtual_balance"] -= size
                                    shared_state["bots"]["BOT_1"]["active_positions"][sym].append({
                                        'entry_price': ask, 'qty': q, 'time': datetime.now(timezone.utc).strftime("%H:%M")
                                    })
                                    add_log(f"[BOT 1] 🚀 شراء {sym} عند {ask}$", "primary")

                # --- البوت الثاني والثالث جاهزان للربط لاحقاً ---
                pass

        except Exception as e:
            add_log(f"خطأ في المحرك: {e}", "warning")

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
    <input type="text" id="u" placeholder="admin" required>
    <input type="password" id="p" placeholder="admin123" required>
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
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Multi-Bot Hub</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f3f4f6;--sub:#94a3b8}
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui}
body{background:var(--bg);color:var(--text);padding:12px}
.tabs{display:flex;gap:6px;margin:12px 0;overflow-x:auto}
.tab{padding:8px 14px;background:#151e30;border:1px solid var(--border);border-radius:8px;color:var(--sub);cursor:pointer;font-weight:bold;white-space:nowrap}
.tab.active{background:var(--primary);color:#fff}
.tab-pane{display:none}
.tab-pane.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:10px}
.val{font-size:18px;font-weight:bold}
.btn{padding:6px 12px;border:none;border-radius:6px;font-weight:bold;cursor:pointer;font-size:12px}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:7px;border-bottom:1px solid var(--border);font-size:12px}
.logs{max-height:160px;overflow-y:auto;font-family:monospace;font-size:11px}
input,select{background:#090d16;border:1px solid var(--border);color:#fff;padding:6px 10px;border-radius:6px;font-size:12px;width:100%}
</style>
</head>
<body>
  <div style="display:flex;justify-content:space-between;align-items:center" class="card">
    <div><strong>🎛️ لوحة إدارة البوتات الثلاثة (MEXC Hub)</strong></div>
    <div style="display:flex;align-items:center;gap:10px">
      <span id="api-stat" style="font-size:12px;color:var(--sub)">جاري الاتصال...</span>
      <button class="btn" style="background:#334155;color:#fff" onclick="fetch('/api/logout').then(()=>location.href='/login')">🚪 خروج</button>
    </div>
  </div>

  <!-- مفاتيح المنصة الموحدة -->
  <details class="card">
    <summary style="cursor:pointer;font-weight:bold;color:#60a5fa">🔑 إعدادات مفاتيح MEXC API الموحدة ▾</summary>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px">
      <input type="password" id="m-key" placeholder="MEXC API Key">
      <input type="password" id="m-sec" placeholder="MEXC API Secret">
    </div>
    <button class="btn" style="background:var(--primary);color:#fff;width:100%;margin-top:8px" onclick="saveKeys()">💾 حفظ المفاتيح</button>
  </details>

  <div class="tabs">
    <button class="tab active" onclick="showTab('t1', this)">🤖 Bot 1 (EWO)</button>
    <button class="tab" onclick="showTab('t2', this)">⚡ Bot 2</button>
    <button class="tab" onclick="showTab('t3', this)">📈 Bot 3</button>
    <button class="tab" onclick="showTab('t4', this)">💰 المحفظة والتسييل</button>
  </div>

  <!-- Bot 1 -->
  <div id="t1" class="tab-pane active">
    <div class="card" style="display:flex;justify-content:space-between;align-items:center">
      <span>الحالة: <strong id="b1-st">RUNNING</strong></span>
      <div>
        <button class="btn" style="background:var(--success);color:#fff" onclick="setSt('BOT_1','RUNNING')">▶️ تشغيل</button>
        <button class="btn" style="background:#f59e0b;color:#000" onclick="setSt('BOT_1','PAUSED')">⏸️ إيقاف مؤقت</button>
      </div>
    </div>
    <div class="grid">
      <div class="card"><div style="font-size:11px;color:var(--sub)">رأس المال الافتراضي</div><div class="val" id="b1-bal">500$</div></div>
      <div class="card"><div style="font-size:11px;color:var(--sub)">أرباح اليوم</div><div class="val" id="b1-pnl">0.00$</div></div>
    </div>
    <div class="card">
      <strong style="font-size:12px">الصفقات المفتوحة (Bot 1):</strong>
      <div style="overflow-x:auto"><table id="b1-orders"><thead><tr><th>العملة</th><th>سعر الدخول</th><th>الكمية</th><th>الوقت</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <!-- Bot 2 -->
  <div id="t2" class="tab-pane">
    <div class="card">
      <p style="color:var(--sub);font-size:13px">⚡ البوت الثاني جاهز في قاعدة البيانات وينتظر تفعيل الاستراتيجية لاحقاً.</p>
    </div>
  </div>

  <!-- Bot 3 -->
  <div id="t3" class="tab-pane">
    <div class="card">
      <p style="color:var(--sub);font-size:13px">📈 البوت الثالث جاهز في قاعدة البيانات وينتظر تفعيل الاستراتيجية لاحقاً.</p>
    </div>
  </div>

  <!-- Wallet -->
  <div id="t4" class="tab-pane">
    <div class="card">
      <div style="overflow-x:auto"><table id="w-table"><thead><tr><th>العملة</th><th>المتاح</th><th>المحجوز</th><th>إجراء تسييل</th></tr></thead><tbody></tbody></table></div>
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
async function saveKeys(){
  await fetch('/api/save_keys',{method:'POST',body:JSON.stringify({api_key:document.getElementById('m-key').value,api_secret:document.getElementById('m-sec').value})});
  alert('✅ تم حفظ مفاتيح MEXC بنجاح!');
  update();
}
async function panic(asset){
  if(confirm(`تسييل ${asset} فورياً بسعر السوق؟`)){
    const r = await fetch('/api/panic',{method:'POST',body:JSON.stringify({asset:asset})});
    const d = await r.json();
    alert(d.msg);
    update();
  }
}
async function update(){
  try{
    const res = await fetch('/api/data');
    if(res.status===401) location.href='/login';
    const d = await res.json();
    
    document.getElementById('api-stat').innerHTML = d.api_connected ? '<span style="color:var(--success)">🟢 MEXC متصل</span>' : '<span style="color:var(--danger)">🔴 API غير متصل</span>';

    // Bot 1
    document.getElementById('b1-bal').innerText = d.bots.BOT_1.virtual_balance.toFixed(2)+'$';
    document.getElementById('b1-pnl').innerText = (d.bots.BOT_1.daily_pnl>=0?'+':'')+d.bots.BOT_1.daily_pnl.toFixed(3)+'$';
    let b1Html = '';
    for(let s in d.bots.BOT_1.active_positions){
      d.bots.BOT_1.active_positions[s].forEach(p=>{ b1Html += `<tr><td>${s}</td><td>${p.entry_price}$</td><td>${p.qty}</td><td>${p.time}</td></tr>`; });
    }
    document.getElementById('b1-orders').querySelector('tbody').innerHTML = b1Html || '<tr><td colspan="4" style="text-align:center;color:var(--sub)">لا توجد صفقات</td></tr>';

    // Wallet
    let wHtml = '';
    (d.wallet_assets||[]).forEach(a=>{
      if(a.free>0 && a.asset!=='USDT') wHtml += `<tr><td>${a.asset}</td><td>${a.free}</td><td>${a.locked}</td><td><button class="btn" style="background:var(--danger);color:#fff;padding:2px 6px" onclick="panic('${a.asset}')">🔥 تسييل</button></td></tr>`;
    });
    document.getElementById('w-table').querySelector('tbody').innerHTML = wHtml || '<tr><td colspan="4" style="text-align:center;color:var(--sub)">لا توجد عملات أو الحساب تجريبي</td></tr>';

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
# 🛡️ خادم الويب ومعالجة المسارات
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

        if self.path == '/api/save_keys':
            database.save_keys(data.get("api_key", ""), data.get("api_secret", ""))
            add_log("تم تحديث مفاتيح MEXC في قاعدة البيانات", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/control':
            database.update_bot_config(data.get("bot_name", "BOT_1"), {"status": data.get("status", "RUNNING")})
            self.send_response(200); self.end_headers()

        elif self.path == '/api/panic':
            asset = data.get("asset")
            free_qty = 0.0
            for a in shared_state.get("wallet_assets", []):
                if a["asset"] == asset: free_qty = float(a["free"]); break
            if free_qty > 0:
                ok, res = place_order(f"{asset}USDT", "SELL", qty=free_qty, is_paper=False)
                msg = f"✅ تم تسييل {asset} بنجاح" if ok else f"❌ فشل التسييل: {res}"
                add_log(f"تسييل {asset}: {msg}", "danger")
            else:
                msg = "لا يوجد رصيد متاح للتسييل"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args): return

if __name__ == "__main__":
    print(f"🚀 بدء التشغيل على المنفذ: {PORT}")
    threading.Thread(target=trading_engine_loop, daemon=True).start()
    with socketserver.TCPServer(("", PORT), WebHandler) as srv:
        srv.serve_forever()
