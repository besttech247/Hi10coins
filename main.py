import http.server
import socketserver
import threading
import json
import secrets
from http import cookies
from datetime import datetime, timezone
import time

# استدعاء الملفات المقسمة
import database
from exchanges import mexc
from strategies import ewo_bot, rsi_bot

PORT = 8080
ACTIVE_SESSIONS = set()

# 1. تهيئة قاعدة البيانات تلقائياً عند الإقلاع
database.init_db()

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

# كائن الحالة المشترك لكافة البوتات
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
        "total_realized_pnl": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
        "active_positions": {sym: [] for sym in SYMBOLS}
    },
    "rsi": {
        "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        "virtual_balance": 500.0,
        "daily_pnl_portfolio": 0.0,
        "total_realized_pnl": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "daily_pnl_coins": {sym: 0.0 for sym in SYMBOLS},
        "active_positions": {sym: [] for sym in SYMBOLS}
    }
}

def add_log(msg, log_type="info"):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    shared_state["recent_logs"].insert(0, {"time": timestamp, "msg": msg, "type": log_type})
    if len(shared_state["recent_logs"]) > 100:
        shared_state["recent_logs"].pop()

# =====================================================================
# 🔄 حلقة تحديث المحفظة اللحظية من MEXC
# =====================================================================
def wallet_sync_loop():
    mexc.fetch_exchange_precisions()
    while True:
        try:
            cfg = database.get_bot_config("EWO_BOT")
            k, s = cfg.get("api_key", "").strip(), cfg.get("api_secret", "").strip()
            if k and s:
                ok, balances = mexc.get_account_balances(k, s)
                if ok:
                    shared_state["api_connected"] = True
                    shared_state["wallet_assets"] = balances
                    for b in balances:
                        if b["asset"] == "USDT":
                            shared_state["real_balance"] = b["free"]
                else:
                    shared_state["api_connected"] = False
        except Exception as e:
            add_log(f"خطأ مزامنة المحفظة: {e}", "warning")
        time.sleep(10)

# =====================================================================
# 🌐 واجهات HTML المدمجة (تسجيل الدخول + لوحة التبويبات)
# =====================================================================
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>تسجيل الدخول - Crypto Hub</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--text:#f3f4f6;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);display:flex;align-items:center;justify-content:center;height:100vh;padding:14px}
.box{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;width:100%;max-width:380px;box-shadow:0 8px 24px #00000044}
h2{font-size:18px;margin-bottom:6px;text-align:center}
p{font-size:12px;color:var(--sub);text-align:center;margin-bottom:18px}
.group{margin-bottom:14px}
label{display:block;font-size:12px;color:var(--sub);margin-bottom:5px}
input{width:100%;padding:10px 12px;background:#090d16;border:1px solid var(--border);border-radius:8px;color:#fff;font-size:13px}
button{width:100%;padding:10px;background:var(--primary);color:#fff;border:none;border-radius:8px;font-weight:bold;cursor:pointer;font-size:14px;margin-top:8px}
.err{color:#ef4444;font-size:12px;text-align:center;margin-top:10px;display:none}
</style>
</head>
<body>
<div class="box">
  <h2>🔐 لوحة القيادة المركزية</h2>
  <p>سجل الدخول لإدارة البوتات والمحفظة</p>
  <form id="f">
    <div class="group"><label>اسم المستخدم</label><input type="text" id="u" placeholder="admin" required></div>
    <div class="group"><label>كلمة المرور</label><input type="password" id="p" placeholder="admin123" required></div>
    <button type="submit">تسجيل الدخول</button>
    <div id="err" class="err">بيانات الدخول غير صحيحة!</div>
  </form>
</div>
<script>
document.getElementById('f').addEventListener('submit', async(e)=>{
  e.preventDefault();
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: document.getElementById('u').value, password: document.getElementById('p').value})
  });
  if(res.ok){ window.location.href = '/'; } else { document.getElementById('err').style.display = 'block'; }
});
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Command Hub - Multi-Bot System</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--warning:#f59e0b;--text:#f3f4f6;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:12px;line-height:1.5}
.container{max-width:1020px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:var(--card);border-radius:12px;border:1px solid var(--border);margin-bottom:12px;flex-wrap:wrap;gap:10px}
.nav-tabs{display:flex;gap:8px;margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:8px;overflow-x:auto}
.tab-btn{padding:8px 16px;background:#151e30;border:1px solid var(--border);border-radius:8px;color:var(--sub);cursor:pointer;font-weight:bold;font-size:13px;white-space:nowrap}
.tab-btn.active{background:var(--primary);color:#fff;border-color:var(--primary)}
.tab-content{display:none}
.tab-content.active{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px}
.card-title{font-size:11px;color:var(--sub);margin-bottom:4px}
.card-val{font-size:20px;font-weight:bold}
.btn{padding:6px 12px;border:none;border-radius:8px;font-weight:bold;cursor:pointer;font-size:12px}
.btn-run{background:var(--success);color:#fff}
.btn-pause{background:var(--warning);color:#000}
.btn-stop{background:var(--danger);color:#fff}
.btn-panic{background:var(--danger);color:#fff;font-size:11px;padding:3px 8px}
.btn-buy{background:var(--primary);color:#fff;font-size:11px;padding:3px 8px}
.btn-copy{background:#334155;color:#fff;font-size:11px;padding:3px 7px}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:8px 10px;font-size:12px;border-bottom:1px solid var(--border)}
th{color:var(--sub)}
.table-wrap{overflow-x:auto}
details{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:12px;overflow:hidden}
summary{padding:10px 14px;cursor:pointer;font-weight:bold;font-size:13px;background:#151e30;display:flex;justify-content:space-between;align-items:center}
.details-content{padding:12px}
.logs{max-height:200px;overflow-y:auto;font-family:monospace;font-size:11px}
.log-row{padding:3px 0;border-bottom:1px solid #1f293d44}
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
input,select{background:#090d16;border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:6px;font-size:12px;width:100%}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h2 style="font-size:16px">🎛️ Command Hub (Modular Multi-Bot Engine)</h2>
      <p style="font-size:11px;color:var(--sub)">نظام إدارة وتداول العملات المتعدد على السيرفر السحابي</p>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <span id="api-status" style="font-size:11px;color:var(--sub)">فحص API...</span>
      <button class="btn" style="background:#334155;color:#fff" onclick="logout()">🚪 خروج</button>
    </div>
  </div>

  <!-- شريط التبويبات -->
  <div class="nav-tabs">
    <button class="tab-btn active" onclick="switchTab('tab-ewo', this)">🤖 EWO Momentum Bot</button>
    <button class="tab-btn" onclick="switchTab('tab-rsi', this)">⚡ RSI Scalper Bot</button>
    <button class="tab-btn" onclick="switchTab('tab-wallet', this)">💰 المحفظة والتسييل الطارئ</button>
  </div>

  <!-- 1. تبويب EWO Bot -->
  <div id="tab-ewo" class="tab-content active">
    <div class="header" style="background:#151e30">
      <div><strong>حالة EWO Bot:</strong> <span id="ewo-status-text">RUNNING</span></div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-run" onclick="setBotStatus('EWO_BOT','RUNNING')">▶️ تشغيل</button>
        <button class="btn btn-pause" onclick="setBotStatus('EWO_BOT','PAUSED')">⏸️ إيقاف مؤقت</button>
        <button class="btn btn-stop" onclick="setBotStatus('EWO_BOT','STOPPED')">⏹️ إيقاف</button>
      </div>
    </div>
    <div class="grid">
      <div class="card"><div class="card-title">الرصيد المتاح (EWO)</div><div class="card-val" id="ewo-bal">0.00$</div></div>
      <div class="card"><div class="card-title">أرباح اليوم المحققة</div><div class="card-val" id="ewo-pnl">+0.00$</div></div>
      <div class="card"><div class="card-title">نسبة النجاح</div><div class="card-val" id="ewo-winrate">0.0%</div></div>
    </div>
    <details>
      <summary style="color:#60a5fa"><span>⚙️ إعدادات EWO Momentum</span><span>تعديل ▾</span></summary>
      <div class="details-content">
        <div class="form-grid">
          <div><label style="font-size:11px;color:var(--sub)">النمط</label><select id="ewo-mode"><option value="1">تجريبي</option><option value="0">حقيقي</option></select></div>
          <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="ewo-size" value="10"></div>
          <div><label style="font-size:11px;color:var(--sub)">API Key</label><input type="password" id="mexc-k"></div>
          <div><label style="font-size:11px;color:var(--sub)">API Secret</label><input type="password" id="mexc-s"></div>
        </div>
        <button class="btn" style="background:var(--primary);color:#fff;margin-top:8px;width:100%" onclick="saveCfg('EWO_BOT', 'ewo')">💾 حفظ الإعدادات</button>
      </div>
    </details>
    <details open>
      <summary><span>📂 صفقات EWO المفتوحة</span><span id="ewo-pos-count">0 صفقات</span></summary>
      <div class="details-content table-wrap">
        <table><thead><tr><th>العملة</th><th>سعر الدخول</th><th>السعر الحالي</th><th>الكمية</th><th>الوقت</th></tr></thead><tbody id="ewo-positions-body"></tbody></table>
      </div>
    </details>
  </div>

  <!-- 2. تبويب RSI Bot -->
  <div id="tab-rsi" class="tab-content">
    <div class="header" style="background:#151e30">
      <div><strong>حالة RSI Bot:</strong> <span id="rsi-status-text">PAUSED</span></div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-run" onclick="setBotStatus('RSI_BOT','RUNNING')">▶️ تشغيل</button>
        <button class="btn btn-pause" onclick="setBotStatus('RSI_BOT','PAUSED')">⏸️ إيقاف مؤقت</button>
        <button class="btn btn-stop" onclick="setBotStatus('RSI_BOT','STOPPED')">⏹️ إيقاف</button>
      </div>
    </div>
    <div class="grid">
      <div class="card"><div class="card-title">الرصيد المتاح (RSI)</div><div class="card-val" id="rsi-bal">0.00$</div></div>
      <div class="card"><div class="card-title">أرباح اليوم المحققة</div><div class="card-val" id="rsi-pnl">+0.00$</div></div>
      <div class="card"><div class="card-title">نسبة النجاح</div><div class="card-val" id="rsi-winrate">0.0%</div></div>
    </div>
    <details>
      <summary style="color:#a78bfa"><span>⚙️ إعدادات RSI Scalper</span><span>تعديل ▾</span></summary>
      <div class="details-content">
        <div class="form-grid">
          <div><label style="font-size:11px;color:var(--sub)">النمط</label><select id="rsi-mode"><option value="1">تجريبي</option><option value="0">حقيقي</option></select></div>
          <div><label style="font-size:11px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="rsi-size" value="10"></div>
        </div>
        <button class="btn" style="background:var(--primary);color:#fff;margin-top:8px;width:100%" onclick="saveCfg('RSI_BOT', 'rsi')">💾 حفظ الإعدادات</button>
      </div>
    </details>
    <details open>
      <summary><span>📂 صفقات RSI المفتوحة</span><span id="rsi-pos-count">0 صفقات</span></summary>
      <div class="details-content table-wrap">
        <table><thead><tr><th>العملة</th><th>سعر الدخول</th><th>السعر الحالي</th><th>الكمية</th><th>الوقت</th></tr></thead><tbody id="rsi-positions-body"></tbody></table>
      </div>
    </details>
  </div>

  <!-- 3. تبويب المحفظة العامة -->
  <div id="tab-wallet" class="tab-content">
    <div class="card" style="margin-bottom:10px">
      <div class="table-wrap">
        <table>
          <thead><tr><th>العملة</th><th>المتاح (Free)</th><th>المحجوز</th><th>الإجمالي</th><th>إجراء تسييل</th></tr></thead>
          <tbody id="wallet-body"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- سجل العمليات العام -->
  <details open>
    <summary><span>📜 سجل الأحداث والعمليات الفورية لجميع البوتات</span><button class="btn btn-copy" onclick="event.stopPropagation();copyLogs()">📋 نسخ السجلات</button></summary>
    <div class="details-content"><div class="logs" id="logs-box"></div></div>
  </details>
</div>

<script>
let logsText = "";
function switchTab(id, btn){
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
function copyLogs(){
  navigator.clipboard.writeText(logsText).then(()=>alert("✅ تم نسخ السجلات!"));
}
async function logout(){
  await fetch('/api/logout', {method:'POST'});
  window.location.href = '/login';
}
async function setBotStatus(botName, status){
  await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bot_name: botName, status: status})
  });
  refresh();
}
async function saveCfg(botName, prefix){
  const payload = {
    bot_name: botName,
    paper_trading: parseInt(document.getElementById(prefix+'-mode').value),
    trade_size_usdt: parseFloat(document.getElementById(prefix+'-size').value)||10
  };
  if(prefix==='ewo'){
    payload.api_key = document.getElementById('mexc-k').value;
    payload.api_secret = document.getElementById('mexc-s').value;
  }
  await fetch('/api/save_config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  alert("✅ تم حفظ الإعدادات في SQLite!");
  refresh();
}
async function panicSell(asset){
  if(confirm(`هل أنت متأكد من تسييل ${asset} فورياً بسعر السوق؟`)){
    const res = await fetch('/api/panic_sell', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({asset: asset})
    });
    const d = await res.json();
    alert(d.msg || JSON.stringify(d));
    refresh();
  }
}

async function refresh(){
  try{
    const res = await fetch('/api/data');
    if(res.status === 401){ window.location.href = '/login'; return; }
    const d = await res.json();

    document.getElementById('api-status').innerHTML = d.api_connected ? '<span style="color:var(--success)">🟢 MEXC متصل</span>' : '<span style="color:var(--danger)">🔴 API غير متصل</span>';

    // EWO Data
    const ewoBal = d.ewo.virtual_balance;
    document.getElementById('ewo-bal').innerText = ewoBal.toFixed(2)+'$';
    document.getElementById('ewo-pnl').innerText = (d.ewo.daily_pnl_portfolio>=0?'+':'')+d.ewo.daily_pnl_portfolio.toFixed(3)+'$';
    document.getElementById('ewo-winrate').innerText = d.ewo.total_trades>0 ? ((d.ewo.winning_trades/d.ewo.total_trades)*100).toFixed(1)+'%' : '0.0%';
    
    let ewoPosHtml = '';
    let ewoCount = 0;
    for(const sym of Object.keys(d.ewo.active_positions)){
      (d.ewo.active_positions[sym]||[]).forEach(p=>{
        ewoCount++;
        const cur = d.market_prices[sym]?.bid || p.entry_price;
        ewoPosHtml += `<tr><td><strong>${sym}</strong></td><td>${p.entry_price.toFixed(4)}$</td><td>${cur.toFixed(4)}$</td><td>${p.qty}</td><td>${p.time}</td></tr>`;
      });
    }
    document.getElementById('ewo-pos-count').innerText = `${ewoCount} صفقات`;
    document.getElementById('ewo-positions-body').innerHTML = ewoPosHtml || '<tr><td colspan="5" style="text-align:center;color:var(--sub)">لا توجد صفقات مفتوحة لـ EWO</td></tr>';

    // RSI Data
    const rsiBal = d.rsi.virtual_balance;
    document.getElementById('rsi-bal').innerText = rsiBal.toFixed(2)+'$';
    document.getElementById('rsi-pnl').innerText = (d.rsi.daily_pnl_portfolio>=0?'+':'')+d.rsi.daily_pnl_portfolio.toFixed(3)+'$';
    document.getElementById('rsi-winrate').innerText = d.rsi.total_trades>0 ? ((d.rsi.winning_trades/d.rsi.total_trades)*100).toFixed(1)+'%' : '0.0%';
    
    let rsiPosHtml = '';
    let rsiCount = 0;
    for(const sym of Object.keys(d.rsi.active_positions)){
      (d.rsi.active_positions[sym]||[]).forEach(p=>{
        rsiCount++;
        const cur = d.market_prices[sym]?.bid || p.entry_price;
        rsiPosHtml += `<tr><td><strong>${sym}</strong></td><td>${p.entry_price.toFixed(4)}$</td><td>${cur.toFixed(4)}$</td><td>${p.qty}</td><td>${p.time}</td></tr>`;
      });
    }
    document.getElementById('rsi-pos-count').innerText = `${rsiCount} صفقات`;
    document.getElementById('rsi-positions-body').innerHTML = rsiPosHtml || '<tr><td colspan="5" style="text-align:center;color:var(--sub)">لا توجد صفقات مفتوحة لـ RSI</td></tr>';

    // Wallet
    let wHtml = '';
    if(d.wallet_assets && d.wallet_assets.length>0){
      d.wallet_assets.forEach(a=>{
        const canSell = a.asset!=='USDT' && a.free>0;
        wHtml += `<tr><td><strong>${a.asset}</strong></td><td>${a.free}</td><td>${a.locked}</td><td>${a.total}</td><td>${canSell?`<button class="btn btn-panic" onclick="panicSell('${a.asset}')">🔥 تسييل</button>`:'-'}</td></tr>`;
      });
    } else {
      wHtml = '<tr><td colspan="5" style="text-align:center;color:var(--sub)">لا توجد أرصدة ظاهرة أو الحساب في الوضع التجريبي</td></tr>';
    }
    document.getElementById('wallet-body').innerHTML = wHtml;

    // Logs
    let lHtml = '';
    logsText = '';
    for(const l of d.recent_logs){
      lHtml += `<div class="log-row"><span style="color:var(--sub)">[${l.time}]</span> <span>${l.msg}</span></div>`;
      logsText += `[${l.time}] ${l.msg}\\n`;
    }
    document.getElementById('logs-box').innerHTML = lHtml || '<div style="color:var(--sub)">في انتظار السجلات...</div>';
  }catch(e){}
}
setInterval(refresh, 2500);
refresh();
</script>
</body>
</html>"""

# =====================================================================
# 🛡️ خادم الويب ومعالجة الطلبات
# =====================================================================
class AuthenticatedServer(http.server.BaseHTTPRequestHandler):
    def is_auth(self):
        cookie = self.headers.get('Cookie')
        if not cookie: return False
        c = cookies.SimpleCookie(cookie)
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
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(shared_state, ensure_ascii=False).encode('utf-8'))
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
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
            else:
                self.send_response(401); self.end_headers()
            return

        if not self.is_auth():
            self.send_response(401); self.end_headers(); return

        if self.path == '/api/logout':
            self.send_response(200); self.send_header('Set-Cookie', 'session_id=; Path=/; Max-Age=0'); self.end_headers()

        elif self.path == '/api/save_config':
            bot_name = data.get("bot_name", "EWO_BOT")
            updates = {}
            if "paper_trading" in data: updates["paper_trading"] = int(data["paper_trading"])
            if "trade_size_usdt" in data: updates["trade_size_usdt"] = float(data["trade_size_usdt"])
            if data.get("api_key"): updates["api_key"] = data["api_key"].strip()
            if data.get("api_secret"): updates["api_secret"] = data["api_secret"].strip()
            
            database.update_bot_config(bot_name, updates)
            add_log(f"تم تحديث إعدادات {bot_name} في قاعدة البيانات", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/control':
            bot_name = data.get("bot_name", "EWO_BOT")
            status = data.get("status", "RUNNING")
            database.update_bot_config(bot_name, {"status": status})
            add_log(f"تغيير حالة {bot_name} إلى: {status}", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/panic_sell':
            asset = data.get("asset")
            cfg = database.get_bot_config("EWO_BOT")
            k, s = cfg.get("api_key", "").strip(), cfg.get("api_secret", "").strip()
            
            free_qty = 0.0
            for a in shared_state.get("wallet_assets", []):
                if a["asset"] == asset:
                    free_qty = a["free"]
                    break

            if free_qty > 0 and k and s:
                ok, res = mexc.place_order(k, s, f"{asset}USDT", "SELL", qty=free_qty, is_paper=False)
                msg = f"✅ تم تسييل {asset} بنجاح!" if ok else f"❌ فشل التسييل: {res}"
                add_log(f"تسييل طارئ لـ {asset}: {msg}", "danger")
            else:
                msg = "لا يوجد رصيد حر أو مفاتيح API غير متوفرة"

            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        return

def run_server():
    with socketserver.TCPServer(("", PORT), AuthenticatedServer) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    print("="*60)
    print(f"🚀 الخادم السحابي جاهز على المنفذ: {PORT}")
    print(f"🔑 المدير الافتراضي: admin / admin123")
    print("="*60)

    # 1. تشغيل خادم الويب
    threading.Thread(target=run_server, daemon=True).start()

    # 2. تشغيل مزامنة المحفظة
    threading.Thread(target=wallet_sync_loop, daemon=True).start()

    # 3. تشغيل استراتيجية بوت EWO
    threading.Thread(target=ewo_bot.run_ewo_loop, args=(shared_state, database.get_bot_config, add_log), daemon=True).start()

    # 4. تشغيل استراتيجية بوت RSI
    threading.Thread(target=rsi_bot.run_rsi_loop, args=(shared_state, database.get_bot_config, add_log), daemon=True).start()

    while True:
        time.sleep(1)
