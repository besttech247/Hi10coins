import os
import urllib.request
import json
import time
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==========================================
# 1. الإعدادات العامة للمحفظة والاستراتيجية
# ==========================================
CAPITAL = 500.0
TRADE_SIZE = 50.0
MAX_CONCURRENT_PER_SYM = 3
STOP_LOSS_PCT = 0.49
DAILY_TARGET_CAP = 5.0
LOOP_INTERVAL = 15
DISCONNECT_THRESHOLD = 35

TARGET_SYMBOLS = ["PAXGUSDT", "XAUTUSDT"]

# ==========================================
# 2. حالة البوت
# ==========================================
bot_state = {
    "is_running": True,
    "server_alive": True,
    "gold_price": 0.0,
    "last_gold_sync": "N/A",
    "market_open": True,
    "daily_realized_pnl": 0.0,
    "total_realized_pnl": 0.0,
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "positions": [],
    "trade_history": [],
    "ticker_data": {},
    "last_signal": "None",
    "candle_lock": {},
    "status": "Online 🟢",
    "last_successful_loop": time.time(),
    "total_downtime_sec": 0,
    "outages": []
}

lock = threading.Lock()

# ==========================================
# 3. دوال جلب البيانات
# ==========================================
def fetch_gold_candles():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=5m&range=2d"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=6) as res:
            data = json.loads(res.read().decode('utf-8'))
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            q = result['indicators']['quote'][0]
            candles = []
            for t, c, h, l in zip(timestamps, q['close'], q['high'], q['low']):
                if c is not None and h is not None and l is not None:
                    candles.append({
                        "time": datetime.fromtimestamp(t, timezone.utc),
                        "close": c,
                        "hl2": (h + l) / 2.0
                    })
            return candles
    except Exception as e:
        raise ConnectionError(f"Yahoo Feed: {e}")

def fetch_mexc_ticker(symbol):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url_bin = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}"
        req_b = urllib.request.Request(url_bin, headers=headers)
        with urllib.request.urlopen(req_b, timeout=4) as res:
            data = json.loads(res.read().decode('utf-8'))
            if 'price' in data:
                p = float(data['price'])
                return {"bid": round(p * 0.9999, 2), "ask": round(p * 1.0001, 2)}
    except Exception:
        pass

    try:
        url_mexc = "https://api.mexc.com/api/v3/ticker/price"
        req_m = urllib.request.Request(url_mexc, headers=headers)
        with urllib.request.urlopen(req_m, timeout=5) as res:
            data = json.loads(res.read().decode('utf-8'))
            for item in data:
                if item.get('symbol') == symbol:
                    p = float(item['price'])
                    return {"bid": round(p * 0.9999, 2), "ask": round(p * 1.0001, 2)}
    except Exception:
        pass

    raise ConnectionError(f"Market Feed ({symbol}) unavailable")

def calculate_ewo(candles):
    if len(candles) < 35:
        return None, None, None
    hl2_list = [c['hl2'] for c in candles]
    ewo_vals = []
    for offset in [3, 2, 1]:
        idx = len(candles) - offset
        sma5 = sum(hl2_list[idx-4:idx+1]) / 5.0
        sma35 = sum(hl2_list[idx-34:idx+1]) / 35.0
        ewo_vals.append(sma5 - sma35)
    return ewo_vals[2], ewo_vals[1], ewo_vals[0]

def is_market_open():
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()
    hour = now_utc.hour
    if weekday == 4 and hour >= 22: return False
    if weekday == 5: return False
    if weekday == 6 and hour < 22: return False
    return True

def format_duration(seconds):
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0: return f"{hours}h {mins}m {secs}s"
    elif mins > 0: return f"{mins}m {secs}s"
    else: return f"{secs}s"

# ==========================================
# 4. محرك التداول
# ==========================================
def trading_engine():
    print("🚀 محرك التداول يعمل على Railway...")
    last_loop_start = time.time()
    
    while bot_state["server_alive"]:
        if not bot_state["is_running"]:
            time.sleep(2)
            last_loop_start = time.time()
            continue

        loop_start = time.time()
        gap = loop_start - last_loop_start
        if gap > DISCONNECT_THRESHOLD:
            outage_duration = gap - LOOP_INTERVAL
            outage_time = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
            with lock:
                bot_state["total_downtime_sec"] += outage_duration
                bot_state["outages"].insert(0, {
                    "time": outage_time,
                    "duration": format_duration(outage_duration),
                    "reason": "إعادة تشغيل الحاوية / انقطاع"
                })

        try:
            today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            with lock:
                if today_str != bot_state["current_day"]:
                    bot_state["current_day"] = today_str
                    bot_state["daily_realized_pnl"] = 0.0
                    bot_state["candle_lock"].clear()
                bot_state["market_open"] = is_market_open()

            tickers = {}
            for sym in TARGET_SYMBOLS:
                try:
                    tk = fetch_mexc_ticker(sym)
                    if tk: tickers[sym] = tk
                except Exception:
                    pass

            candles = fetch_gold_candles()
            
            if tickers and candles:
                with lock:
                    bot_state["status"] = "Online 🟢" if bot_state["is_running"] else "Paused ⏸️"
                    bot_state["last_successful_loop"] = time.time()
                    bot_state["ticker_data"] = tickers

                last_candle = candles[-1]
                ewo_now, ewo_p1, ewo_p2 = calculate_ewo(candles)
                
                with lock:
                    bot_state["gold_price"] = last_candle["close"]
                    bot_state["last_gold_sync"] = last_candle["time"].strftime('%H:%M:%S UTC')
                
                buy_signal = (ewo_now is not None and ewo_now < 0 and ewo_now > ewo_p1 and ewo_p1 <= ewo_p2)
                sell_signal = (ewo_now is not None and ewo_now > 0 and ewo_now < ewo_p1)
                
                with lock:
                    if buy_signal: bot_state["last_signal"] = "BUY REBOUND"
                    elif sell_signal: bot_state["last_signal"] = "SELL MOMENTUM"
                    else: bot_state["last_signal"] = "NEUTRAL"

                # فحص الخروج
                with lock:
                    remaining_pos = []
                    for pos in bot_state["positions"]:
                        sym = pos["symbol"]
                        if sym in tickers:
                            curr_ask = tickers[sym]["ask"]
                            pnl_pct = ((curr_ask - pos["entry_price"]) / pos["entry_price"]) * 100.0
                            hit_sl = pnl_pct <= -STOP_LOSS_PCT
                            hit_tp = sell_signal
                            
                            if hit_sl or hit_tp:
                                pnl_usd = (pnl_pct / 100.0) * TRADE_SIZE
                                bot_state["daily_realized_pnl"] += pnl_usd
                                bot_state["total_realized_pnl"] += pnl_usd
                                bot_state["trade_history"].insert(0, {
                                    "symbol": sym,
                                    "entry_time": pos["time"],
                                    "exit_time": datetime.now(timezone.utc).strftime('%H:%M:%S'),
                                    "entry_p": pos["entry_price"],
                                    "exit_p": curr_ask,
                                    "pnl_pct": pnl_pct,
                                    "pnl_usd": pnl_usd,
                                    "reason": "SL" if hit_sl else "TP"
                                })
                            else:
                                remaining_pos.append(pos)
                        else:
                            remaining_pos.append(pos)
                    bot_state["positions"] = remaining_pos

                # فحص الدخول
                if bot_state["market_open"] and buy_signal and bot_state["is_running"]:
                    with lock:
                        target_ok = bot_state["daily_realized_pnl"] < DAILY_TARGET_CAP
                        candle_id = last_candle["time"].strftime('%Y%m%d%H%M')
                        if target_ok:
                            for sym in TARGET_SYMBOLS:
                                if sym in tickers:
                                    sym_count = len([p for p in bot_state["positions"] if p["symbol"] == sym])
                                    not_locked = bot_state["candle_lock"].get(sym) != candle_id
                                    if sym_count < MAX_CONCURRENT_PER_SYM and not_locked:
                                        entry_maker_price = tickers[sym]["bid"]
                                        bot_state["positions"].append({
                                            "symbol": sym,
                                            "time": datetime.now(timezone.utc).strftime('%H:%M:%S'),
                                            "entry_price": entry_maker_price,
                                            "size_usd": TRADE_SIZE
                                        })
                                        bot_state["candle_lock"][sym] = candle_id
                                        print(f"✅ [ENTRY MAKER] {sym} @ {entry_maker_price}$")

        except ConnectionError:
            with lock: bot_state["status"] = "Offline / Feed Error 🔴"
        except Exception as e:
            print(f"❌ خطأ: {e}")
            
        last_loop_start = time.time()
        time.sleep(LOOP_INTERVAL)

# ==========================================
# 5. لوحة التحكم
# ==========================================
class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

HTML_TEMPLATE = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gold Trading Bot - Railway</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 15px; }
        .card { background: #1e293b; border-radius: 12px; padding: 15px; margin-bottom: 15px; border: 1px solid #334155; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
        .stat-box { background: #0f172a; padding: 10px; border-radius: 8px; text-align: center; }
        .val { font-size: 1.15rem; font-weight: bold; margin-top: 5px; }
        .green { color: #22c55e; } .red { color: #ef4444; } .gold { color: #eab308; } .orange { color: #f97316; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.85rem; }
        th, td { padding: 8px; text-align: right; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; }
        .badge { padding: 3px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: bold; }
        .badge-buy { background: #166534; color: #86efac; }
        .badge-sell { background: #991b1b; color: #fca5a5; }
        .badge-neu { background: #334155; color: #cbd5e1; }
        .btn { padding: 8px 16px; border-radius: 8px; border: none; font-weight: bold; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn-pause { background: #eab308; color: #0f172a; }
        .btn-resume { background: #22c55e; color: #ffffff; }
    </style>
    <script>setTimeout(() => { location.reload(); }, 6000);</script>
</head>
<body>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <h2 style="margin:0;">🟡 لوحة تحكم تداول الذهب (Railway)</h2>
        <a href="${ACTION_URL}" class="btn ${ACTION_CLASS}">${ACTION_TEXT}</a>
    </div>

    <div class="card">
        <div class="grid">
            <div class="stat-box">
                <div style="color:#94a3b8; font-size:0.8rem;">حالة البوت</div>
                <div class="val">${BOT_STATUS}</div>
            </div>
            <div class="stat-box">
                <div style="color:#94a3b8; font-size:0.8rem;">سعر الذهب (XAU)</div>
                <div class="val gold">${GOLD_PRICE}</div>
            </div>
            <div class="stat-box">
                <div style="color:#94a3b8; font-size:0.8rem;">أرباح اليوم</div>
                <div class="val ${DAILY_COLOR}">${DAILY_PNL}$</div>
            </div>
            <div class="stat-box">
                <div style="color:#94a3b8; font-size:0.8rem;">إجمالي وقت التوقف</div>
                <div class="val orange">${TOTAL_DOWNTIME}</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">📡 حالة الاتصال وسجل الانقطاعات (${OUTAGE_COUNT})</h3>
        <div style="font-size: 0.85rem; color: #94a3b8; margin-bottom: 8px;">
            آخر فحص: <span style="color:#f8fafc;">${LAST_HEARTBEAT}</span> | حالة السوق: ${MARKET_STATUS}
        </div>
        <table>
            <thead><tr><th>وقت الاستعادة</th><th>مدة التوقف</th><th>السبب</th></tr></thead>
            <tbody>${OUTAGES_ROWS}</tbody>
        </table>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">📊 أسعار العملات على MEXC وإشارة EWO</h3>
        <div style="margin-bottom: 8px;">الإشارة: <span class="badge ${SIGNAL_BADGE}">${LAST_SIGNAL}</span> | آخر شمعة: ${LAST_SYNC}</div>
        <table>
            <thead><tr><th>العملة</th><th>سعر الشراء (Bid)</th><th>سعر البيع (Ask)</th></tr></thead>
            <tbody>${TICKERS_ROWS}</tbody>
        </table>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">📂 الصفقات المفتوحة (${OPEN_COUNT})</h3>
        <table>
            <thead><tr><th>الرمز</th><th>وقت الدخول</th><th>سعر الدخول</th><th>الربح اللحظي</th></tr></thead>
            <tbody>${POSITIONS_ROWS}</tbody>
        </table>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">📜 سجل الصفقات المغلقة (آخر 5)</h3>
        <table>
            <thead><tr><th>الرمز</th><th>الخروج</th><th>الربح %</th><th>الربح $</th><th>السبب</th></tr></thead>
            <tbody>${HISTORY_ROWS}</tbody>
        </table>
    </div>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/toggle':
            with lock:
                bot_state["is_running"] = not bot_state["is_running"]
                bot_state["status"] = "Online 🟢" if bot_state["is_running"] else "Paused ⏸️"
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
            return

        with lock:
            daily_color = "green" if bot_state["daily_realized_pnl"] >= 0 else "red"
            market_text = '<span class="green">مفتوح 🟢</span>' if bot_state["market_open"] else '<span class="red">مغلق 🔴</span>'
            sig = bot_state["last_signal"]
            sig_badge = "badge-buy" if "BUY" in sig else ("badge-sell" if "SELL" in sig else "badge-neu")
            
            sec_since_hb = int(time.time() - bot_state["last_successful_loop"])
            last_hb_str = f"منذ {sec_since_hb} ثانية" if sec_since_hb < 60 else f"منذ {sec_since_hb//60} دقيقة"

            act_url = "/toggle"
            act_text = "⏸️ إيقاف مؤقت" if bot_state["is_running"] else "▶️ تشغيل التداول"
            act_class = "btn-pause" if bot_state["is_running"] else "btn-resume"

            o_rows = ""
            for out in bot_state["outages"][:5]:
                o_rows += f"<tr><td>{out['time']}</td><td class='orange'>{out['duration']}</td><td>{out['reason']}</td></tr>"
            if not o_rows:
                o_rows = "<tr><td colspan='3' style='color:#22c55e;'>لم يتم رصد أي انقطاع ✨</td></tr>"

            t_rows = ""
            for sym, tk in bot_state["ticker_data"].items():
                t_rows += f"<tr><td><b>{sym}</b></td><td>{tk['bid']:.2f}$</td><td>{tk['ask']:.2f}$</td></tr>"
            if not t_rows: t_rows = "<tr><td colspan='3'>جاري جلب الأسعار...</td></tr>"

            p_rows = ""
            for p in bot_state["positions"]:
                sym = p["symbol"]
                curr_ask = bot_state["ticker_data"].get(sym, {}).get("ask", p["entry_price"])
                upnl = ((curr_ask - p["entry_price"]) / p["entry_price"]) * 100.0
                u_color = "green" if upnl >= 0 else "red"
                p_rows += f"<tr><td><b>{sym}</b></td><td>{p['time']}</td><td>{p['entry_price']:.2f}$</td><td class='{u_color}'>{upnl:+.2f}%</td></tr>"
            if not p_rows: p_rows = "<tr><td colspan='4'>لا توجد صفقات مفتوحة حالياً</td></tr>"

            h_rows = ""
            for h in bot_state["trade_history"][:5]:
                h_col = "green" if h["pnl_usd"] >= 0 else "red"
                h_rows += f"<tr><td><b>{h['symbol']}</b></td><td>{h['exit_time']}</td><td class='{h_col}'>{h['pnl_pct']:+.2f}%</td><td class='{h_col}'>{h['pnl_usd']:+.3f}$</td><td>{h['reason']}</td></tr>"
            if not h_rows: h_rows = "<tr><td colspan='5'>لا توجد صفقات مغلقة بعد</td></tr>"

            html = HTML_TEMPLATE.replace("${ACTION_URL}", act_url) \
                                .replace("${ACTION_TEXT}", act_text) \
                                .replace("${ACTION_CLASS}", act_class) \
                                .replace("${BOT_STATUS}", bot_state["status"]) \
                                .replace("${GOLD_PRICE}", f"{bot_state['gold_price']:.2f}") \
                                .replace("${MARKET_STATUS}", market_text) \
                                .replace("${DAILY_PNL}", f"{bot_state['daily_realized_pnl']:+.3f}") \
                                .replace("${DAILY_COLOR}", daily_color) \
                                .replace("${TOTAL_DOWNTIME}", format_duration(bot_state["total_downtime_sec"])) \
                                .replace("${LAST_HEARTBEAT}", last_hb_str) \
                                .replace("${OUTAGE_COUNT}", str(len(bot_state["outages"]))) \
                                .replace("${OUTAGES_ROWS}", o_rows) \
                                .replace("${LAST_SIGNAL}", sig) \
                                .replace("${SIGNAL_BADGE}", sig_badge) \
                                .replace("${LAST_SYNC}", bot_state["last_gold_sync"]) \
                                .replace("${TICKERS_ROWS}", t_rows) \
                                .replace("${OPEN_COUNT}", str(len(bot_state["positions"]))) \
                                .replace("${POSITIONS_ROWS}", p_rows) \
                                .replace("${HISTORY_ROWS}", h_rows)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def log_message(self, format, *args):
        return

def run_server():
    # قراءة المنفذ المخصص من Railway تلقائياً
    port = int(os.environ.get("PORT", 8080))
    server = ReusableHTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f"🌐 خادم الويب يعمل على المنفذ: {port}")
    server.serve_forever()

if __name__ == '__main__':
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    trading_engine()
