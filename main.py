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
from concurrent.futures import ThreadPoolExecutor
from http import cookies
from datetime import datetime, timezone
import database

PORT = int(os.environ.get("PORT", 8080))
database.init_db()

BASE_URL = "https://api.mexc.com"
FUTURES_URL = "https://contract.mexc.com"
ACTIVE_SESSIONS = set()
ssl_ctx = ssl._create_unverified_context()
START_TIME = time.time()

SYMBOL_RULES = {}
UNSUPPORTED_API_SYMBOLS = set()
MIN_SELL_NOTIONAL_USDT = 1.0
LAST_ENTRY_CANDLE = {}
BOT_KEYS = ["BOT_1", "BOT_X1", "BOT_X2", "BOT_X3", "BOT_EWO_MTF", "BOT_EWO_MTFH"]
CLASSIC_BOTS = ["BOT_1"]
EXPERIMENTAL_BOTS = ["BOT_X1", "BOT_X2", "BOT_X3"]
MTF_BOT = "BOT_EWO_MTF"
MTFH_BOT = "BOT_EWO_MTFH"
MTF_BOTS = [MTF_BOT, MTFH_BOT]
MTF_TF_ORDER = ["5m", "15m", "30m", "60m", "4h", "1d"]
MTF_TF_LABELS = {"5m": "5m", "15m": "15m", "30m": "30m", "60m": "1h", "4h": "4h", "1d": "1d"}

shared_state = {
    "api_connected": False,
    "has_saved_keys": False,
    "masked_key": "",
    "server_public_ip": "جاري الجلب...",
    "real_balance_usdt": 0.0,
    "total_wallet_usd_value": 0.0,
    "total_live_pnl": 0.0,
    "wallet_assets": [],
    "market_prices": {},
    "recent_logs": [],
    "ops_alerts": [],
    "open_limit_orders": [],
    "sniper_positions": [],
    "start_timestamp": START_TIME,
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "bots": {}
}

for k in BOT_KEYS:
    is_mtf = k in MTF_BOTS
    shared_state["bots"][k] = {
        "name": k,
        "status": "PAUSED",
        "symbols": [],
        "order_exec_type": "CHASE_LIMIT",
        "max_allocation": 300.0 if is_mtf else 50.0,
        "max_concurrent": 2 if is_mtf else 1,
        "trade_size": 15.0 if is_mtf else 10.0,
        "tp_pct": 2.5,
        "sl_pct": 1.2,
        "timeframe": "15m",
        "trailing_stop": 1 if (k in EXPERIMENTAL_BOTS or is_mtf) else 0,
        "trailing_cb": 0.006,
        "mtf_settings": database.DEFAULT_MTF_SETTINGS if is_mtf else {},
        "daily_pnl": 0.0,
        "daily_target": 5.0,
        "daily_loss_limit": 5.0,
        "trades_count": 0,
        "winning_count": 0,
        "daily_pnl_coins": {},
        "active_positions": {}
    }

def get_current_iso_time():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def init_trades_from_db():
    try:
        rows = database.load_all_active_trades()
        for r in rows:
            bKey = r.get("bot_name")
            sym = r.get("symbol")
            if bKey in shared_state["bots"]:
                if sym not in shared_state["bots"][bKey]["active_positions"]:
                    shared_state["bots"][bKey]["active_positions"][sym] = []
                meta = r.get("meta") or {}
                shared_state["bots"][bKey]["active_positions"][sym].append({
                    "id": r.get("id"),
                    "entry_price": float(r.get("entry_price", 0.0)),
                    "highest_price": float(r.get("highest_price", r.get("entry_price", 0.0))),
                    "qty": float(r.get("qty", 0.0)),
                    "tp_pct": float(r.get("tp_pct", 0.025)),
                    "sl_pct": float(r.get("sl_pct", 0.012)),
                    "time": r.get("time_str", "--:--"),
                    "timeframe": r.get("timeframe") or meta.get("timeframe", ""),
                    "meta": meta,
                    "entry_fee_rate": meta.get("entry_fee_rate"),
                    "be_armed": bool(meta.get("be_armed", False)),
                    "trail_armed": bool(meta.get("trail_armed", False)),
                })
        shared_state["sniper_positions"] = database.load_sniper_trades()
    except Exception:
        pass

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

_ALERT_COOLDOWN = {}

def push_ops_alert(kind, msg, cooldown_sec=180):
    """Surface operational problems in UI + live log, with cooldown to avoid spam."""
    now = time.time()
    key = f"{kind}:{msg[:100]}"
    last = _ALERT_COOLDOWN.get(key, 0)
    if now - last < cooldown_sec:
        return
    _ALERT_COOLDOWN[key] = now
    alert = {
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "kind": kind,
        "msg": msg
    }
    alerts = shared_state.setdefault("ops_alerts", [])
    alerts.insert(0, alert)
    shared_state["ops_alerts"] = alerts[:40]
    add_log(f"🚨 [{kind}] {msg}", "system", "danger")

def bot_entries_allowed(bot_state):
    """Gate new entries by daily profit target and daily loss limit."""
    pnl = float(bot_state.get("daily_pnl", 0.0))
    profit_cap = float(bot_state.get("daily_target", 5.0))
    loss_cap = abs(float(bot_state.get("daily_loss_limit", 5.0)))
    if pnl >= profit_cap:
        return False, "profit_target"
    if pnl <= -loss_cap:
        return False, "loss_limit"
    return True, None

def _fmt_setting_val(v):
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)

def summarize_bot_config_changes(bot_name, before, after):
    """Build detailed Arabic change lines for the live log."""
    lines = []
    label_map = {
        "max_allocation_usdt": "سقف رأس المال",
        "max_concurrent_per_coin": "أقصى صفقات/عملة",
        "trade_size_usdt": "حجم الصفقة",
        "order_exec_type": "نوع الأمر",
        "timeframe": "الفريم",
        "tp_pct": "TP",
        "sl_pct": "SL",
        "trailing_stop": "Trailing",
        "trailing_cb": "Trailing CB",
        "daily_profit_target": "هدف ربح يومي",
        "daily_loss_limit": "حد خسارة يومي",
        "status": "الحالة",
        "symbols": "الرموز",
    }
    for key, label in label_map.items():
        if key not in after:
            continue
        old = before.get(key)
        new = after.get(key)
        if old is None and new is None:
            continue
        try:
            if isinstance(new, (int, float)) or isinstance(old, (int, float)):
                if float(old or 0) == float(new or 0):
                    continue
            elif str(old) == str(new):
                continue
        except Exception:
            if str(old) == str(new):
                continue
        # Show percents more readably for tp/sl/cb
        if key in ("tp_pct", "sl_pct", "trailing_cb") and new is not None:
            try:
                old_s = f"{float(old or 0)*100:.2f}%"
                new_s = f"{float(new)*100:.2f}%"
            except Exception:
                old_s, new_s = _fmt_setting_val(old), _fmt_setting_val(new)
        else:
            old_s, new_s = _fmt_setting_val(old), _fmt_setting_val(new)
        lines.append(f"{label}: {old_s} → {new_s}")

    old_mtf = before.get("mtf_settings") if isinstance(before.get("mtf_settings"), dict) else {}
    new_mtf = after.get("mtf_settings") if isinstance(after.get("mtf_settings"), dict) else {}
    if new_mtf:
        old_g = (old_mtf or {}).get("_global") or {}
        new_g = new_mtf.get("_global") or {}
        for gk, glabel in (
            ("max_open_positions", "أقصى صفقات مفتوحة"),
            ("be_offset", "BE offset"),
            ("ewo_exit_min_profit", "EWO exit min"),
        ):
            if gk in new_g and float(old_g.get(gk, -1) or -1) != float(new_g.get(gk) or 0):
                if gk in ("be_offset", "ewo_exit_min_profit"):
                    lines.append(f"{glabel}: {float(old_g.get(gk, 0))*100:.2f}% → {float(new_g.get(gk, 0))*100:.2f}%")
                else:
                    lines.append(f"{glabel}: {old_g.get(gk)} → {new_g.get(gk)}")

        for tf in MTF_TF_ORDER:
            o = (old_mtf or {}).get(tf) or {}
            n = new_mtf.get(tf) or {}
            if not n:
                continue
            tf_lab = MTF_TF_LABELS.get(tf, tf)
            checks = [
                ("enabled", "تفعيل"),
                ("trade_size_usdt", "حجم"),
                ("tp_pct", "TP"),
                ("sl_pct", "SL"),
                ("be_enabled", "BE"),
                ("be_trigger_pct", "BE@"),
                ("trail_enabled", "Trail"),
                ("trail_trigger_pct", "Trail@"),
                ("trail_cb_pct", "TrailCB"),
                ("hierarchy_parent", "فلتر هرمي"),
            ]
            for ck, clabel in checks:
                if ck not in n:
                    continue
                ov, nv = o.get(ck), n.get(ck)
                changed = False
                if ck in ("tp_pct", "sl_pct", "be_trigger_pct", "trail_trigger_pct", "trail_cb_pct"):
                    try:
                        changed = abs(float(ov or 0) - float(nv or 0)) > 1e-12
                    except Exception:
                        changed = str(ov) != str(nv)
                elif ck == "hierarchy_parent":
                    changed = database.normalize_hierarchy_parent(tf, ov) != database.normalize_hierarchy_parent(tf, nv)
                else:
                    changed = ov != nv
                if not changed:
                    continue
                if ck == "hierarchy_parent":
                    old_p = database.normalize_hierarchy_parent(tf, ov)
                    new_p = database.normalize_hierarchy_parent(tf, nv)
                    old_s = "off" if old_p == "off" else MTF_TF_LABELS.get(old_p, old_p)
                    new_s = "off" if new_p == "off" else MTF_TF_LABELS.get(new_p, new_p)
                    lines.append(f"[{tf_lab}] {clabel}: {old_s} → {new_s}")
                elif ck in ("tp_pct", "sl_pct", "be_trigger_pct", "trail_trigger_pct", "trail_cb_pct"):
                    lines.append(f"[{tf_lab}] {clabel}: {float(ov or 0)*100:.2f}% → {float(nv or 0)*100:.2f}%")
                elif ck in ("enabled", "be_enabled", "trail_enabled"):
                    lines.append(f"[{tf_lab}] {clabel}: {'on' if ov else 'off'} → {'on' if nv else 'off'}")
                else:
                    lines.append(f"[{tf_lab}] {clabel}: {ov} → {nv}")

    if not lines:
        return [f"تم حفظ إعدادات {bot_name} (بدون تغيير ملحوظ)"]
    return [f"تم حفظ إعدادات {bot_name}:"] + lines

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
    if symbol in SYMBOL_RULES:
        return
    try:
        url = f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=4) as res:
            d = json.loads(res.read().decode('utf-8'))
            for s in d.get("symbols", []):
                if s["symbol"] == symbol:
                    base_prec = int(s.get("baseAssetPrecision", 2))
                    quote_prec = int(s.get("quotePrecision", 4))
                    status = str(s.get("status", "ENABLED")).upper()
                    supported = status in ("ENABLED", "1", "TRUE", "")
                    SYMBOL_RULES[symbol] = {
                        "base_prec": base_prec,
                        "quote_prec": quote_prec,
                        "supported": supported
                    }
                    if not supported:
                        UNSUPPORTED_API_SYMBOLS.add(symbol)
                    return
            # exchange answered but symbol missing → not API-tradable
            SYMBOL_RULES[symbol] = {"base_prec": 2, "quote_prec": 4, "supported": False}
            UNSUPPORTED_API_SYMBOLS.add(symbol)
    except Exception:
        # Network/parse failure: keep provisional rules, do not blacklist permanently
        SYMBOL_RULES[symbol] = {"base_prec": 2, "quote_prec": 4, "supported": True}

def symbol_api_supported(symbol):
    if symbol in UNSUPPORTED_API_SYMBOLS:
        return False
    update_exchange_info(symbol)
    return bool(SYMBOL_RULES.get(symbol, {}).get("supported", True))

def asset_to_usdt_symbol(asset):
    asset = str(asset or "").strip()
    if not asset or asset in ("USDT", "USDC"):
        return None
    # Skip non-standard wallet names like GOLD(XAUT)
    if any(ch in asset for ch in "()[]{}/\\ "):
        return None
    return f"{asset}USDT"

def can_market_sell_wallet_asset(asset_row, min_notional=MIN_SELL_NOTIONAL_USDT):
    """
    Pre-check before dust/panic sells.
    Returns (True, symbol) or (False, reason_code).
    """
    asset = asset_row.get("asset")
    free_qty = float(asset_row.get("free", 0.0) or 0.0)
    val_usd = float(asset_row.get("usd_value", 0.0) or 0.0)
    if asset in ("USDT", "USDC"):
        return False, "stable"
    if free_qty <= 0:
        return False, "zero"
    sym = asset_to_usdt_symbol(asset)
    if not sym:
        return False, "bad_symbol"
    if val_usd < float(min_notional):
        return False, "below_min"
    if not symbol_api_supported(sym):
        return False, "unsupported"
    return True, sym

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

def fetch_klines(symbol, interval="15m", limit=45):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=6) as res:
            data = json.loads(res.read().decode('utf-8'))
            return [{
                'time': int(r[0]), 'open': float(r[1]), 'high': float(r[2]),
                'low': float(r[3]), 'close': float(r[4]), 'vol': float(r[5])
            } for r in data]
    except Exception:
        return []

def fetch_futures_klines(symbol, interval="Min5"):
    try:
        contract_sym = symbol.replace("USDT", "_USDT") if not symbol.endswith("_USDT") else symbol
        url = f"{FUTURES_URL}/api/v1/contract/kline/{contract_sym}?interval={interval}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=6) as res:
            res_data = json.loads(res.read().decode('utf-8'))
            if res_data.get("success") and "data" in res_data:
                d = res_data["data"]
                times = d.get("time", [])
                opens = d.get("open", [])
                highs = d.get("high", [])
                lows = d.get("low", [])
                closes = d.get("close", [])
                vols = d.get("vol", [])
                
                candles = []
                for i in range(len(times)):
                    candles.append({
                        'time': int(times[i]),
                        'open': float(opens[i]),
                        'high': float(highs[i]),
                        'low': float(lows[i]),
                        'close': float(closes[i]),
                        'vol': float(vols[i])
                    })
                return candles
    except Exception:
        pass
    return []

def calculate_rsi(candles, period=14):
    if len(candles) < period + 1: return 50.0
    closes = [c['close'] for c in candles]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

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

def candle_vol_mult(candles):
    if not candles or len(candles) < 5:
        return 0.0
    vols = [c['vol'] for c in candles[:-1]]
    avg_vol = sum(vols[-20:]) / max(len(vols[-20:]), 1)
    cur_vol = candles[-1]['vol']
    return (cur_vol / avg_vol) if avg_vol > 0 else 0.0

def bot_x_htf_ok(symbol, htf="60m"):
    """Higher-timeframe momentum confirmation for experimental bot."""
    candles = fetch_klines(symbol, interval=htf, limit=45)
    if not candles:
        return False
    e3, e2, e1 = calculate_ewo(candles)
    if e1 is None or e2 is None:
        return False
    return (e1 >= e2) or (e1 > 0)

def bot_x_entry_ok(candles, rsi_th=42.0, vol_th=1.5):
    """EWO rebound + (volume spike OR RSI pullback)."""
    e3, e2, e1 = calculate_ewo(candles)
    if e1 is None or e2 is None or e3 is None:
        return False
    sig_rebound = (e1 < 0 and e1 > e2 and e2 <= e3)
    if not sig_rebound:
        return False
    rsi_val = calculate_rsi(candles, 14)
    vol_mult = candle_vol_mult(candles)
    green = candles[-1]['close'] >= candles[-1]['open']
    sig_vol = (vol_mult >= vol_th) and green
    sig_rsi = rsi_val <= rsi_th
    return sig_vol or sig_rsi

def ewo_rebound_signal(candles):
    e3, e2, e1 = calculate_ewo(candles)
    if e1 is None or e2 is None or e3 is None:
        return False, (None, None, None)
    return (e1 < 0 and e1 > e2 and e2 <= e3), (e3, e2, e1)

def mtf_tf_supportive(candles):
    """Higher-TF trend support: rising EWO or already positive."""
    e3, e2, e1 = calculate_ewo(candles)
    if e1 is None or e2 is None:
        return False
    return (e1 >= e2) or (e1 > 0)

def mtf_hierarchy_ok(symbol, entry_tf, mtf_settings):
    """
    MTFH confirm: entry TF checks only its configured hierarchy_parent
    (off, or one strictly higher TF such as 15m/30m/60m/4h/1d).
    """
    tf_cfg = mtf_settings.get(entry_tf) or {}
    parent = database.normalize_hierarchy_parent(entry_tf, tf_cfg.get("hierarchy_parent", "off"))
    if parent == "off":
        return True
    candles = fetch_klines(symbol, interval=parent, limit=45)
    if not candles:
        return False
    return mtf_tf_supportive(candles)

def get_mtf_settings(cfg_or_bot=None):
    if isinstance(cfg_or_bot, dict) and cfg_or_bot.get("mtf_settings"):
        raw = cfg_or_bot.get("mtf_settings")
        if isinstance(raw, dict):
            return database.parse_mtf_settings(raw)
        return database.parse_mtf_settings(raw)
    return database.parse_mtf_settings(database.DEFAULT_MTF_SETTINGS)

def position_notional(pos):
    meta = pos.get("meta") or {}
    if meta.get("trade_size_usdt"):
        return float(meta.get("trade_size_usdt"))
    return float(pos.get("entry_price", 0)) * float(pos.get("qty", 0))

# Spot fee model: maker/chase ~0%, market/taker ~0.1% per side (estimate).
MARKET_FEE_RATE = 0.001
MAKER_FEE_RATE = 0.0

def tag_order_fee_mode(res, fee_mode):
    if isinstance(res, dict):
        out = dict(res)
        out["_fee_mode"] = fee_mode
        return out
    return res

def resolve_fee_rate(exec_type=None, order_res=None):
    """Prefer tagged/actual order mode; else bot exec_type; else market."""
    if isinstance(order_res, dict):
        mode = str(order_res.get("_fee_mode") or "").upper()
        if mode in ("MARKET", "TAKER"):
            return MARKET_FEE_RATE
        if mode in ("CHASE_LIMIT", "LIMIT", "MAKER"):
            return MAKER_FEE_RATE
        ot = str(order_res.get("type") or "").upper()
        if ot == "MARKET":
            return MARKET_FEE_RATE
        if ot == "LIMIT":
            return MAKER_FEE_RATE
    et = str(exec_type or "").upper()
    if et == "CHASE_LIMIT":
        return MAKER_FEE_RATE
    return MARKET_FEE_RATE

def resolve_fill(res, qty_hint=None, price_fallback=None):
    """Average fill price and filled qty from exchange response."""
    fallback_px = float(price_fallback) if price_fallback else None
    hint_qty = float(qty_hint) if qty_hint else 0.0
    if not isinstance(res, dict):
        return fallback_px, hint_qty
    quote = float(res.get("cummulativeQuoteQty") or 0.0)
    filled = float(res.get("executedQty") or 0.0)
    if filled <= 0 and hint_qty > 0:
        filled = hint_qty
    if quote > 0 and filled > 0:
        return quote / filled, filled
    return fallback_px, filled if filled > 0 else hint_qty

def clean_price(price, decimals=8):
    """Remove binary float noise while keeping exchange-relevant precision."""
    try:
        return float(f"{float(price):.{int(decimals)}f}")
    except (TypeError, ValueError):
        return price

def clean_qty(qty, decimals=8):
    try:
        return float(f"{float(qty):.{int(decimals)}f}")
    except (TypeError, ValueError):
        return qty

def fmt_usd(price, decimals=8):
    try:
        s = f"{clean_price(price, decimals):.{int(decimals)}f}".rstrip("0").rstrip(".")
        return s if s else "0"
    except (TypeError, ValueError):
        return str(price)

def build_entry_from_fill(res, price_fallback, qty_hint, exec_type):
    avg, filled = resolve_fill(res, qty_hint=qty_hint, price_fallback=price_fallback)
    entry = clean_price(avg if avg is not None else price_fallback)
    qty = clean_qty(filled if filled and filled > 0 else qty_hint)
    return entry, qty, resolve_fee_rate(exec_type, res)

def position_entry_fee_rate(pos, fallback_exec_type=None):
    if pos.get("entry_fee_rate") is not None:
        try:
            return float(pos.get("entry_fee_rate"))
        except (TypeError, ValueError):
            pass
    meta = pos.get("meta") or {}
    if meta.get("entry_fee_rate") is not None:
        try:
            return float(meta.get("entry_fee_rate"))
        except (TypeError, ValueError):
            pass
    return resolve_fee_rate(fallback_exec_type)

def calc_round_trip_pnl(entry, exit_px, qty, entry_fee_rate, exit_fee_rate):
    entry = float(entry)
    exit_px = float(exit_px)
    qty = float(qty)
    gross = (exit_px - entry) * qty
    fee = (entry * qty * float(entry_fee_rate)) + (exit_px * qty * float(exit_fee_rate))
    return gross, fee, gross - fee

def settle_exit_pnl(entry, qty, order_res, bid_fallback, entry_fee_rate, exec_type=None):
    exit_px, filled = resolve_fill(order_res, qty_hint=qty, price_fallback=bid_fallback or entry)
    if exit_px is None:
        exit_px = float(bid_fallback or entry)
    sell_qty = float(filled if filled and filled > 0 else qty)
    exit_fee = resolve_fee_rate(exec_type, order_res)
    gross, fee, net = calc_round_trip_pnl(entry, exit_px, sell_qty, entry_fee_rate, exit_fee)
    return exit_px, sell_qty, gross, fee, net

def evaluate_coin_signals(ticker, source="FUTURES", tf="5m", vol_th=2.0, rsi_th=38.0):
    raw_sym = ticker["symbol"]
    spot_sym = raw_sym.replace("_", "")
    coin_source = ticker.get("source", source)

    if coin_source == "FUTURES":
        fut_tf = "Min1" if tf == "1m" else ("Min5" if tf == "5m" else ("Min15" if tf == "15m" else "Min60"))
        candles = fetch_futures_klines(raw_sym, interval=fut_tf)
        if not candles or len(candles) < 25:
            candles = fetch_klines(spot_sym, interval=tf, limit=38)
    else:
        candles = fetch_klines(spot_sym, interval=tf, limit=38)

    if not candles or len(candles) < 25: return None

    candles_1d = fetch_klines(spot_sym, interval="1d", limit=2)
    candles_1h = fetch_klines(spot_sym, interval="60m", limit=2)

    cur_price = candles[-1]['close']
    
    change_d_pct = 0.0
    if len(candles_1d) >= 2 and candles_1d[-2]['close'] > 0:
        prev_d_close = candles_1d[-2]['close']
        change_d_pct = ((cur_price - prev_d_close) / prev_d_close) * 100.0

    change_1h_pct = 0.0
    if len(candles_1h) >= 2 and candles_1h[-2]['close'] > 0:
        prev_1h_close = candles_1h[-2]['close']
        change_1h_pct = ((cur_price - prev_1h_close) / prev_1h_close) * 100.0

    vols = [c['vol'] for c in candles[:-1]]
    avg_vol = sum(vols[-20:]) / 20.0 if len(vols) >= 20 else 1.0
    cur_vol = candles[-1]['vol']
    vol_mult = (cur_vol / avg_vol) if avg_vol > 0 else 0.0
    sig_vol = (vol_mult >= vol_th) and (candles[-1]['close'] >= candles[-1]['open'])

    rsi_val = calculate_rsi(candles, 14)
    sig_rsi = (rsi_val <= rsi_th) and (candles[-1]['close'] > candles[-2]['close'])

    e3, e2, e1 = calculate_ewo(candles)
    sig_ewo = False
    if e1 is not None and e2 is not None:
        sig_ewo = (e1 > e2) and ((e2 < 0 and e1 >= e2) or (e2 <= 0 and e1 > 0))

    signals_count = sum([1 if sig_vol else 0, 1 if sig_rsi else 0, 1 if sig_ewo else 0])
    if signals_count == 0: return None

    return {
        "symbol": spot_sym,
        "source": coin_source,
        "price": cur_price,
        "change_d_pct": round(change_d_pct, 2),
        "change_1h_pct": round(change_1h_pct, 2),
        "sig_vol": sig_vol,
        "vol_mult": round(vol_mult, 1),
        "sig_rsi": sig_rsi,
        "rsi_val": round(rsi_val, 1),
        "sig_ewo": sig_ewo,
        "signals_count": signals_count
    }

def get_asset_free_balance(asset_name):
    for a in shared_state.get("wallet_assets", []):
        if a["asset"] == asset_name:
            return float(a.get("free", 0.0))
    return 0.0

def get_total_bot_allocated_qty(asset_name):
    sym = f"{asset_name}USDT"
    tot = 0.0
    for b in BOT_KEYS:
        positions = shared_state["bots"][b]["active_positions"].get(sym, [])
        for p in positions:
            tot += float(p.get("qty", 0.0))
    for sp in shared_state.get("sniper_positions", []):
        if sp.get("symbol") == sym:
            tot += float(sp.get("qty", 0.0))
    return tot

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
    
    price_info = f" بسعر {price}$" if price else (f" بقيمة {quote_qty}$" if quote_qty else f" بكمية {qty}")
    add_log(f"📤 طلب {side.upper()} {symbol} ({order_type}){price_info}", "orders", "info")
    ok, res = mexc_private_request("/api/v3/order", method="POST", params=params)
    if not ok:
        err_s = str(res)
        if "10007" in err_s or "not support api" in err_s.lower():
            UNSUPPORTED_API_SYMBOLS.add(symbol)
            if symbol in SYMBOL_RULES:
                SYMBOL_RULES[symbol]["supported"] = False
        add_log(f"❌ خطأ {symbol}: {res}", "orders", "danger")
        return ok, res
    fee_mode = "MARKET" if order_type.upper() == "MARKET" else "LIMIT"
    return True, tag_order_fee_mode(res, fee_mode)

def execute_smart_chase_order(symbol, side, qty=None, quote_qty=None):
    g_settings = database.get_global_settings()
    max_chase_secs = int(g_settings.get("chase_timeout", 12))
    interval_secs = float(g_settings.get("chase_interval", 2.0))

    def market_fallback(reason="timeout"):
        push_ops_alert(
            "chase_fallback",
            f"Chase→Market على {symbol} {side} ({reason})",
            cooldown_sec=60
        )
        ok_m, res_m = place_order(symbol, side, qty=qty, quote_qty=quote_qty, order_type="MARKET")
        if ok_m:
            return True, tag_order_fee_mode(res_m, "MARKET")
        return ok_m, res_m

    bid, ask = get_orderbook(symbol)
    if not bid or not ask:
        return market_fallback("no_orderbook")
    
    order_price = bid if side.upper() == "BUY" else ask
    order_qty = qty if qty else (quote_qty / order_price if quote_qty else 0)
    
    ok, res = place_order(symbol, side, qty=order_qty, price=order_price, order_type="LIMIT")
    if not ok:
        return market_fallback("limit_rejected")
    
    order_id = res.get("orderId")
    start_t = time.time()
    
    while time.time() - start_t < max_chase_secs:
        time.sleep(interval_secs)
        cur_bid, cur_ask = get_orderbook(symbol)
        best_price = cur_bid if side.upper() == "BUY" else cur_ask
        
        ok_chk, ord_info = mexc_private_request("/api/v3/order", params={"symbol": symbol, "orderId": order_id})
        if ok_chk and ord_info.get("status") == "FILLED":
            return True, tag_order_fee_mode(ord_info, "CHASE_LIMIT")
        
        if best_price != order_price:
            mexc_private_request("/api/v3/order", method="DELETE", params={"symbol": symbol, "orderId": order_id})
            order_price = best_price
            ok_re, res_re = place_order(symbol, side, qty=order_qty, price=order_price, order_type="LIMIT")
            if ok_re:
                order_id = res_re.get("orderId")
            else:
                break

    mexc_private_request("/api/v3/order", method="DELETE", params={"symbol": symbol, "orderId": order_id})
    return market_fallback("timeout")

def refresh_wallet_and_prices():
    try:
        all_active_symbols = set()
        for bKey in BOT_KEYS:
            cfg = database.get_bot_config(bKey)
            syms = parse_symbols_list(cfg.get("symbols", ""))
            shared_state["bots"][bKey]["symbols"] = syms
            for s in syms: all_active_symbols.add(s)

        for sp in shared_state.get("sniper_positions", []):
            all_active_symbols.add(sp["symbol"])

        ok, acc = mexc_private_request("/api/v3/account")
        if ok and isinstance(acc, dict) and "balances" in acc:
            shared_state["api_connected"] = True
            for b in acc["balances"]:
                total = float(b["free"]) + float(b["locked"])
                asset = b["asset"]
                if total > 0.0 and asset != "USDT":
                    all_active_symbols.add(f"{asset}USDT")

            for sym in all_active_symbols:
                bid, ask = get_orderbook(sym)
                if bid and ask:
                    shared_state["market_prices"][sym] = {"bid": bid, "ask": ask}

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
                    bot_alloc = get_total_bot_allocated_qty(asset)
                    unlinked_free = max(0.0, free - bot_alloc)
                    assets.append({
                        "asset": asset, "free": free, "locked": locked, "total": total,
                        "bot_alloc": bot_alloc, "unlinked_free": unlinked_free,
                        "usd_price": usd_price, "usd_value": val_usd
                    })
                if asset == "USDT": usdt_free = free
            shared_state["wallet_assets"] = assets
            shared_state["real_balance_usdt"] = usdt_free
            shared_state["total_wallet_usd_value"] = total_val_usd
        else:
            shared_state["api_connected"] = False
            push_ops_alert("api", "فشل الاتصال بـ MEXC account / المفاتيح أو الشبكة", cooldown_sec=300)

        ok_ord, open_ords = mexc_private_request("/api/v3/openOrders")
        if ok_ord and isinstance(open_ords, list):
            shared_state["open_limit_orders"] = open_ords

        total_pnl = 0.0
        for b in BOT_KEYS:
            for s, pos_list in shared_state["bots"][b]["active_positions"].items():
                curBid = shared_state["market_prices"].get(s, {}).get("bid", 0.0)
                if curBid:
                    for p in pos_list:
                        total_pnl += (curBid - p["entry_price"]) * p["qty"]
        
        for sp in shared_state.get("sniper_positions", []):
            curBid = shared_state["market_prices"].get(sp["symbol"], {}).get("bid", 0.0)
            if curBid:
                total_pnl += (curBid - sp["entry_price"]) * sp["qty"]

        shared_state["total_live_pnl"] = total_pnl

    except Exception:
        pass

def trading_engine_loop():
    fetch_server_ip()
    init_trades_from_db()
    time.sleep(2)
    add_log(f"محرك التداول نشط (IP: {shared_state['server_public_ip']})", "system", "info")
    
    while True:
        try:
            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != shared_state["current_day"]:
                shared_state["current_day"] = now_day
                for bKey in BOT_KEYS:
                    shared_state["bots"][bKey]["daily_pnl"] = 0.0
                    for s in shared_state["bots"][bKey]["daily_pnl_coins"]:
                        shared_state["bots"][bKey]["daily_pnl_coins"][s] = 0.0
                add_log(f"🌅 تصفير الأهداف اليومية ({now_day} UTC)", "system", "info")

            refresh_wallet_and_prices()

            # متابعة صفقات القناص المستقلة وحساب الأرباح الفعلية
            still_snipers = []
            for sp in shared_state.get("sniper_positions", []):
                sym = sp["symbol"]
                p_info = shared_state["market_prices"].get(sym)
                if not p_info or not p_info["bid"]:
                    still_snipers.append(sp)
                    continue

                bid = p_info["bid"]
                entry = sp["entry_price"]
                highest = sp.get("highest_price", entry)
                base_asset = sym.replace("USDT", "").replace("USDC", "")
                prof_name = sp.get("sniper_profile", "SNIPER_1")

                if bid > highest:
                    highest = bid
                    sp["highest_price"] = highest
                    database.update_sniper_trade(sp["id"], {"highest_price": highest})

                tp1_price = entry * (1.0 + sp.get("tp1_pct", 0.015))
                tp2_price = entry * (1.0 + sp.get("tp2_pct", 0.030))
                sl_price = entry * (1.0 - sp.get("sl_pct", 0.010))

                if not sp.get("tp1_hit") and (highest >= tp1_price):
                    half_qty = float(format_quantity(sym, sp["qty"] * 0.5))
                    if half_qty > 0:
                        ok, res = place_order(sym, "SELL", qty=half_qty, order_type="MARKET")
                        if ok:
                            entry_fee = position_entry_fee_rate(sp, "MARKET")
                            real_exit, sold_qty, gross_pnl, fee_usd, net_pnl = settle_exit_pnl(
                                entry, half_qty, res, bid, entry_fee, "MARKET"
                            )

                            database.archive_closed_trade({
                                "id": f"{sp['id']}_tp1", "bot_name": prof_name, "symbol": sym,
                                "entry_price": entry, "exit_price": real_exit,
                                "qty": sold_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                "net_pnl": net_pnl, "reason": "🎯 TP1 (50% تأمين)",
                                "entry_time": sp["time_str"], "exit_time": get_current_iso_time()
                            })

                            sp["tp1_hit"] = 1
                            sp["qty"] -= sold_qty
                            database.update_sniper_trade(sp["id"], {"tp1_hit": 1, "qty": sp["qty"]})
                            add_log(f"🎯 [{prof_name}] بيع 50% لـ {sym} عند {real_exit}$ | ربح: {net_pnl:+.3f}$ وتأمين الدخول", "sells", "success")

                effective_sl = entry if sp.get("tp1_hit") else sl_price
                cb_pct = sp.get("trailing_cb", 0.006)
                trailing_sl = highest * (1.0 - cb_pct)
                if sp.get("tp1_hit") and highest >= (entry * (1.0 + cb_pct)):
                    effective_sl = max(effective_sl, trailing_sl)

                hit_tp2 = bid >= tp2_price
                hit_sl = bid <= effective_sl

                if hit_tp2 or hit_sl:
                    reason = "🎯 TP2 النهائي" if hit_tp2 else ("🔄 TS القناص" if effective_sl > sl_price else "🛑 SL القناص")
                    avail = get_asset_free_balance(base_asset)
                    sell_qty = min(sp["qty"], avail)

                    if float(format_quantity(sym, sell_qty)) > 0:
                        ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")
                        if ok:
                            entry_fee = position_entry_fee_rate(sp, "MARKET")
                            real_exit, sold_qty, gross_pnl, fee_usd, net_pnl = settle_exit_pnl(
                                entry, sell_qty, res, bid, entry_fee, "MARKET"
                            )

                            database.archive_closed_trade({
                                "id": sp["id"], "bot_name": prof_name, "symbol": sym,
                                "entry_price": entry, "exit_price": real_exit,
                                "qty": sold_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                "net_pnl": net_pnl, "reason": reason,
                                "entry_time": sp["time_str"], "exit_time": get_current_iso_time()
                            })
                            database.delete_sniper_trade(sp["id"])
                            add_log(f"💰 [{prof_name}] إغلاق نهائي لـ {sym} | خروج: {real_exit}$ | صافي: {net_pnl:+.3f}$ ({reason})", "sells", "success" if net_pnl > 0 else "danger")
                        else:
                            still_snipers.append(sp)
                    else:
                        database.delete_sniper_trade(sp["id"])
                else:
                    still_snipers.append(sp)

            shared_state["sniper_positions"] = still_snipers

            # متابعة صفقات البوتات
            configs = {k: database.get_bot_config(k) for k in BOT_KEYS}
            for bKey, cfg in configs.items():
                syms = parse_symbols_list(cfg.get("symbols", ""))
                shared_state["bots"][bKey]["status"] = cfg.get("status", "PAUSED")
                shared_state["bots"][bKey]["order_exec_type"] = cfg.get("order_exec_type", "CHASE_LIMIT")
                shared_state["bots"][bKey]["max_allocation"] = float(cfg.get("max_allocation_usdt", 50.0))
                shared_state["bots"][bKey]["max_concurrent"] = int(cfg.get("max_concurrent_per_coin", 1))
                shared_state["bots"][bKey]["trade_size"] = float(cfg.get("trade_size_usdt", 10.0))
                shared_state["bots"][bKey]["tp_pct"] = float(cfg.get("tp_pct", 0.025)) * 100.0
                shared_state["bots"][bKey]["sl_pct"] = float(cfg.get("sl_pct", 0.012)) * 100.0
                shared_state["bots"][bKey]["timeframe"] = cfg.get("timeframe", "15m")
                shared_state["bots"][bKey]["trailing_stop"] = int(cfg.get("trailing_stop", 1 if bKey in EXPERIMENTAL_BOTS or bKey in MTF_BOTS else 0))
                shared_state["bots"][bKey]["trailing_cb"] = float(cfg.get("trailing_cb", 0.006))
                shared_state["bots"][bKey]["symbols"] = syms
                shared_state["bots"][bKey]["daily_target"] = float(cfg.get("daily_profit_target", 5.0))
                shared_state["bots"][bKey]["daily_loss_limit"] = abs(float(cfg.get("daily_loss_limit", 5.0)))
                if bKey in MTF_BOTS:
                    shared_state["bots"][bKey]["mtf_settings"] = get_mtf_settings(cfg)
                    shared_state["bots"][bKey]["max_allocation"] = float(cfg.get("max_allocation_usdt", 300.0))

                allowed, lock_reason = bot_entries_allowed(shared_state["bots"][bKey])
                if not allowed and cfg.get("status") == "RUNNING":
                    if lock_reason == "loss_limit":
                        push_ops_alert(
                            "daily_loss",
                            f"{bKey}: توقف الدخول — خسارة اليوم بلغت الحد {shared_state['bots'][bKey]['daily_loss_limit']}$",
                            cooldown_sec=900
                        )
                    elif lock_reason == "profit_target":
                        push_ops_alert(
                            "daily_profit",
                            f"{bKey}: توقف الدخول — تحقق هدف الربح اليومي {shared_state['bots'][bKey]['daily_target']}$",
                            cooldown_sec=900
                        )

                for s in syms:
                    if s not in shared_state["bots"][bKey]["active_positions"]:
                        shared_state["bots"][bKey]["active_positions"][s] = []
                    if s not in shared_state["bots"][bKey]["daily_pnl_coins"]:
                        shared_state["bots"][bKey]["daily_pnl_coins"][s] = 0.0

            for bKey in BOT_KEYS:
                cfg = configs[bKey]
                if cfg.get("status") == "STOPPED": continue

                sym_list = shared_state["bots"][bKey]["symbols"]
                size = float(cfg.get("trade_size_usdt", 10.0))
                max_alloc = shared_state["bots"][bKey]["max_allocation"]
                max_con = shared_state["bots"][bKey]["max_concurrent"]
                exec_type = shared_state["bots"][bKey]["order_exec_type"]
                
                total_open_trades = sum(len(shared_state["bots"][bKey]["active_positions"].get(s, [])) for s in sym_list)
                current_used_cap = total_open_trades * size

                for sym in sym_list:
                    p_info = shared_state["market_prices"].get(sym)
                    if not p_info or not p_info["bid"]: continue
                    bid = p_info["bid"]
                    ask = p_info["ask"]
                    base_asset = sym.replace("USDT", "").replace("USDC", "")

                    if bKey in CLASSIC_BOTS:
                        tf = "5m" if bKey == "BOT_1" else cfg.get("timeframe", "15m")
                        candles = fetch_klines(sym, interval=tf, limit=45)
                        
                        if candles:
                            latest_candle_time = candles[-1]['time']
                            e3, e2, e1 = calculate_ewo(candles)
                            
                            if e1 is not None:
                                default_tp_pct = float(cfg.get("tp_pct", 0.025))
                                default_sl_pct = float(cfg.get("sl_pct", 0.012))
                                still_pos = []
                                
                                for pos in shared_state["bots"][bKey]["active_positions"].get(sym, []):
                                    pos_tp_pct = pos.get("tp_pct", default_tp_pct)
                                    pos_sl_pct = pos.get("sl_pct", default_sl_pct)
                                    
                                    tp_price = pos['entry_price'] * (1.0 + pos_tp_pct)
                                    sl_price = pos['entry_price'] * (1.0 - pos_sl_pct)
                                    hit_tp = bid >= tp_price
                                    hit_sl = bid <= sl_price
                                    hit_rev = (e2 > 0) and (e1 < e2) and (bid >= pos['entry_price'] * 1.004)
                                    
                                    if hit_tp or hit_sl or hit_rev:
                                        reason = "🎯 TP" if hit_tp else ("🛑 SL" if hit_sl else "🔄 EWO")
                                        avail = get_asset_free_balance(base_asset)
                                        sell_qty = min(pos['qty'], avail)

                                        if float(format_quantity(sym, sell_qty)) <= 0:
                                            database.delete_active_trade(pos['id'])
                                            continue

                                        if exec_type == "CHASE_LIMIT":
                                            ok, res = execute_smart_chase_order(sym, "SELL", qty=sell_qty)
                                        else:
                                            ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")

                                        if ok:
                                            entry_fee = position_entry_fee_rate(pos, exec_type)
                                            real_exit, sold_qty, gross_pnl, fee_usd, net_pnl = settle_exit_pnl(
                                                pos['entry_price'], sell_qty, res, bid, entry_fee, exec_type
                                            )
                                            
                                            shared_state["bots"][bKey]["daily_pnl"] += net_pnl
                                            shared_state["bots"][bKey]["daily_pnl_coins"][sym] += net_pnl
                                            shared_state["bots"][bKey]["trades_count"] += 1
                                            if net_pnl > 0: shared_state["bots"][bKey]["winning_count"] += 1
                                            
                                            database.archive_closed_trade({
                                                "id": pos["id"], "bot_name": bKey, "symbol": sym,
                                                "entry_price": pos["entry_price"], "exit_price": real_exit,
                                                "qty": sold_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                                "net_pnl": net_pnl, "reason": reason,
                                                "entry_time": pos["time"], "exit_time": get_current_iso_time()
                                            })
                                            database.delete_active_trade(pos["id"])
                                            add_log(f"💰 [{bKey}] بيع {sym} | خروج: {real_exit}$ | صافي: {net_pnl:+.3f}$ ({reason})", "sells", "success" if net_pnl > 0 else "danger")
                                        else:
                                            if "30005" in str(res) or "Oversold" in str(res):
                                                push_ops_alert("oversold", f"{bKey}: رصيد غير كافٍ لإغلاق {sym} (Oversold) — حُذفت من التتبع")
                                                database.delete_active_trade(pos['id'])
                                            else:
                                                still_pos.append(pos)
                                    else:
                                        still_pos.append(pos)
                                
                                shared_state["bots"][bKey]["active_positions"][sym] = still_pos

                                sig_rebound = (e1 < 0 and e1 > e2 and e2 <= e3)
                                lock_key = f"{bKey}_{sym}"
                                is_candle_locked = (LAST_ENTRY_CANDLE.get(lock_key) == latest_candle_time)
                                can_open_coin = len(still_pos) < max_con
                                can_open_alloc = (current_used_cap + size) <= max_alloc
                                entries_ok, _lock = bot_entries_allowed(shared_state["bots"][bKey])

                                if cfg.get("status") == "RUNNING" and sig_rebound and not is_candle_locked and can_open_coin and can_open_alloc and entries_ok:
                                    if shared_state["real_balance_usdt"] >= size:
                                        q = float(format_quantity(sym, size / ask))
                                        if q > 0:
                                            if exec_type == "CHASE_LIMIT":
                                                ok, res = execute_smart_chase_order(sym, "BUY", quote_qty=size)
                                            else:
                                                ok, res = place_order(sym, "BUY", qty=q, quote_qty=size, order_type="MARKET")
                                            
                                            if ok:
                                                LAST_ENTRY_CANDLE[lock_key] = latest_candle_time
                                                trade_id = f"{bKey.lower()}_{int(time.time()*1000)}"
                                                time_str = get_current_iso_time()
                                                fill_entry, fill_qty, entry_fee = build_entry_from_fill(res, ask, q, exec_type)
                                                t_obj = {
                                                    'id': trade_id, 'bot_name': bKey, 'symbol': sym,
                                                    'entry_price': fill_entry, 'highest_price': fill_entry, 'qty': fill_qty,
                                                    'tp_pct': default_tp_pct, 'sl_pct': default_sl_pct,
                                                    'time_str': time_str,
                                                    'meta': {'entry_fee_rate': entry_fee}
                                                }
                                                database.insert_active_trade(t_obj)
                                                shared_state["bots"][bKey]["active_positions"][sym].append({
                                                    'id': trade_id, 'entry_price': fill_entry, 'highest_price': fill_entry, 'qty': fill_qty,
                                                    'tp_pct': default_tp_pct, 'sl_pct': default_sl_pct, 'time': time_str,
                                                    'entry_fee_rate': entry_fee, 'meta': {'entry_fee_rate': entry_fee}
                                                })
                                                current_used_cap += size
                                                add_log(f"🚀 [{bKey}] شراء {sym} عند {fill_entry}$ ({exec_type})", "buys", "primary")
                                    else:
                                        push_ops_alert("balance", f"{bKey}: رصيد USDT غير كافٍ لشراء {sym} (مطلوب {size}$)", cooldown_sec=300)

                    elif bKey in EXPERIMENTAL_BOTS:
                        tf = cfg.get("timeframe", "15m")
                        candles = fetch_klines(sym, interval=tf, limit=45)
                        if not candles:
                            continue

                        latest_candle_time = candles[-1]['time']
                        default_tp_pct = float(cfg.get("tp_pct", 0.025))
                        default_sl_pct = float(cfg.get("sl_pct", 0.010))
                        use_ts = bool(int(cfg.get("trailing_stop", 1)))
                        cb_pct = float(cfg.get("trailing_cb", 0.006))
                        still_x = []
                        bot_state = shared_state["bots"][bKey]

                        for pos in bot_state["active_positions"].get(sym, []):
                            entry = pos['entry_price']
                            highest = pos.get('highest_price', entry)
                            if bid > highest:
                                highest = bid
                                pos['highest_price'] = highest
                                database.update_active_trade(pos['id'], {"highest_price": highest})

                            pos_tp_pct = pos.get("tp_pct", default_tp_pct)
                            pos_sl_pct = pos.get("sl_pct", default_sl_pct)
                            tp_price = entry * (1.0 + pos_tp_pct)
                            sl_price = entry * (1.0 - pos_sl_pct)
                            trail_armed = use_ts and highest >= (entry * (1.0 + max(cb_pct, 0.008)))
                            trailing_sl = highest * (1.0 - cb_pct) if trail_armed else sl_price
                            effective_sl = max(sl_price, trailing_sl) if trail_armed else sl_price

                            e3, e2, e1 = calculate_ewo(candles)
                            hit_tp = bid >= tp_price
                            hit_sl = bid <= effective_sl
                            hit_rev = (
                                e1 is not None and e2 is not None and
                                (e2 > 0) and (e1 < e2) and
                                (bid >= entry * 1.008)
                            )

                            if hit_tp or hit_sl or hit_rev:
                                reason = "🎯 TP" if hit_tp else ("🔄 TS" if trail_armed and hit_sl and effective_sl > sl_price else ("🔄 EWO+" if hit_rev else "🛑 SL"))
                                avail = get_asset_free_balance(base_asset)
                                sell_qty = min(pos['qty'], avail)
                                if float(format_quantity(sym, sell_qty)) <= 0:
                                    database.delete_active_trade(pos['id'])
                                    continue

                                if exec_type == "CHASE_LIMIT":
                                    ok, res = execute_smart_chase_order(sym, "SELL", qty=sell_qty)
                                else:
                                    ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")

                                if ok:
                                    entry_fee = position_entry_fee_rate(pos, exec_type)
                                    real_exit, sold_qty, gross_pnl, fee_usd, net_pnl = settle_exit_pnl(
                                        entry, sell_qty, res, bid, entry_fee, exec_type
                                    )

                                    bot_state["daily_pnl"] += net_pnl
                                    bot_state["daily_pnl_coins"][sym] = bot_state["daily_pnl_coins"].get(sym, 0.0) + net_pnl
                                    bot_state["trades_count"] += 1
                                    if net_pnl > 0:
                                        bot_state["winning_count"] += 1

                                    database.archive_closed_trade({
                                        "id": pos["id"], "bot_name": bKey, "symbol": sym,
                                        "entry_price": entry, "exit_price": real_exit,
                                        "qty": sold_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                        "net_pnl": net_pnl, "reason": reason,
                                        "entry_time": pos["time"], "exit_time": get_current_iso_time()
                                    })
                                    database.delete_active_trade(pos["id"])
                                    add_log(f"💰 [{bKey}] إغلاق {sym} | خروج: {real_exit}$ | صافي: {net_pnl:+.3f}$ ({reason})", "sells", "success" if net_pnl > 0 else "danger")
                                else:
                                    if "30005" in str(res) or "Oversold" in str(res):
                                        push_ops_alert("oversold", f"{bKey}: رصيد غير كافٍ لإغلاق {sym} (Oversold) — حُذفت من التتبع")
                                        database.delete_active_trade(pos['id'])
                                    else:
                                        still_x.append(pos)
                            else:
                                still_x.append(pos)

                        bot_state["active_positions"][sym] = still_x

                        lock_key = f"{bKey}_{sym}"
                        is_candle_locked = (LAST_ENTRY_CANDLE.get(lock_key) == latest_candle_time)
                        can_open_coin = len(still_x) < max_con
                        can_open_alloc = (current_used_cap + size) <= max_alloc
                        entries_ok, _lock = bot_entries_allowed(bot_state)
                        entry_ready = bot_x_entry_ok(candles) and bot_x_htf_ok(sym, "60m")

                        if cfg.get("status") == "RUNNING" and entry_ready and not is_candle_locked and can_open_coin and can_open_alloc and entries_ok:
                            if shared_state["real_balance_usdt"] >= size:
                                q = float(format_quantity(sym, size / ask))
                                if q > 0:
                                    if exec_type == "CHASE_LIMIT":
                                        ok, res = execute_smart_chase_order(sym, "BUY", quote_qty=size)
                                    else:
                                        ok, res = place_order(sym, "BUY", qty=q, quote_qty=size, order_type="MARKET")
                                    if ok:
                                        LAST_ENTRY_CANDLE[lock_key] = latest_candle_time
                                        trade_id = f"{bKey.lower()}_{int(time.time()*1000)}"
                                        time_str = get_current_iso_time()
                                        fill_entry, fill_qty, entry_fee = build_entry_from_fill(res, ask, q, exec_type)
                                        t_obj = {
                                            'id': trade_id, 'bot_name': bKey, 'symbol': sym,
                                            'entry_price': fill_entry, 'highest_price': fill_entry, 'qty': fill_qty,
                                            'tp_pct': default_tp_pct, 'sl_pct': default_sl_pct,
                                            'time_str': time_str,
                                            'meta': {'entry_fee_rate': entry_fee}
                                        }
                                        database.insert_active_trade(t_obj)
                                        bot_state["active_positions"][sym].append({
                                            'id': trade_id, 'entry_price': fill_entry, 'highest_price': fill_entry, 'qty': fill_qty,
                                            'tp_pct': default_tp_pct, 'sl_pct': default_sl_pct, 'time': time_str,
                                            'entry_fee_rate': entry_fee, 'meta': {'entry_fee_rate': entry_fee}
                                        })
                                        current_used_cap += size
                                        add_log(f"🧪 [{bKey}] شراء {sym} عند {fill_entry}$ ({exec_type}) | EWO+HTF+Confirm", "buys", "primary")
                            else:
                                push_ops_alert("balance", f"{bKey}: رصيد USDT غير كافٍ لشراء {sym} (مطلوب {size}$)", cooldown_sec=300)

                    elif bKey in MTF_BOTS:
                        bot_state = shared_state["bots"][bKey]
                        hierarchical = (bKey == MTFH_BOT)
                        mtf = get_mtf_settings(cfg if cfg.get("mtf_settings") else bot_state)
                        gcfg = mtf.get("_global", {})
                        max_open = int(gcfg.get("max_open_positions", 4))
                        be_offset = float(gcfg.get("be_offset", 0.001))
                        ewo_exit_min = float(gcfg.get("ewo_exit_min_profit", 0.008))
                        max_con = int(cfg.get("max_concurrent_per_coin", 2))
                        max_alloc = float(cfg.get("max_allocation_usdt", 300.0))
                        exec_type = cfg.get("order_exec_type", "CHASE_LIMIT")

                        all_pos = []
                        for s2, plist in bot_state["active_positions"].items():
                            for p in plist:
                                all_pos.append((s2, p))
                        open_count = len(all_pos)
                        used_cap = sum(position_notional(p) for _, p in all_pos)

                        # Manage open positions for this symbol across TFs
                        still_sym = []
                        for pos in bot_state["active_positions"].get(sym, []):
                            tf = pos.get("timeframe") or (pos.get("meta") or {}).get("timeframe") or "15m"
                            meta = dict(pos.get("meta") or {})
                            entry = float(pos["entry_price"])
                            highest = float(pos.get("highest_price", entry))
                            if bid > highest:
                                highest = bid
                                pos["highest_price"] = highest
                                meta["highest_price"] = highest
                                database.update_active_trade(pos["id"], {"highest_price": highest, "meta_json": json.dumps(meta, ensure_ascii=False)})

                            tp_pct = float(pos.get("tp_pct", meta.get("tp_pct", 0.022)))
                            sl_pct = float(pos.get("sl_pct", meta.get("sl_pct", 0.010)))
                            be_enabled = bool(meta.get("be_enabled", False))
                            trail_enabled = bool(meta.get("trail_enabled", False))
                            be_trigger = float(meta.get("be_trigger_pct", 0.012))
                            trail_trigger = float(meta.get("trail_trigger_pct", 0.018))
                            trail_cb = float(meta.get("trail_cb_pct", 0.006))
                            be_armed = bool(pos.get("be_armed", meta.get("be_armed", False)))
                            trail_armed = bool(pos.get("trail_armed", meta.get("trail_armed", False)))

                            sl0 = entry * (1.0 - sl_pct)
                            tp_price = entry * (1.0 + tp_pct)
                            sl_eff = sl0

                            if be_enabled and highest >= entry * (1.0 + be_trigger):
                                be_armed = True
                            if trail_enabled and highest >= entry * (1.0 + trail_trigger):
                                trail_armed = True

                            if be_armed:
                                sl_eff = max(sl_eff, entry * (1.0 + be_offset))
                            if trail_armed:
                                sl_eff = max(sl_eff, highest * (1.0 - trail_cb))

                            if be_armed != bool(meta.get("be_armed", False)) or trail_armed != bool(meta.get("trail_armed", False)):
                                meta["be_armed"] = be_armed
                                meta["trail_armed"] = trail_armed
                                pos["be_armed"] = be_armed
                                pos["trail_armed"] = trail_armed
                                pos["meta"] = meta
                                database.update_active_trade(pos["id"], {"meta_json": json.dumps(meta, ensure_ascii=False)})

                            candles_tf = fetch_klines(sym, interval=tf, limit=45)
                            e3, e2, e1 = calculate_ewo(candles_tf) if candles_tf else (None, None, None)
                            hit_tp = bid >= tp_price
                            hit_sl = bid <= sl_eff
                            hit_ewo = (
                                e1 is not None and e2 is not None and
                                (e2 > 0) and (e1 < e2) and
                                (bid >= entry * (1.0 + ewo_exit_min))
                            )

                            if hit_tp or hit_sl or hit_ewo:
                                if hit_tp:
                                    reason = "🎯 TP"
                                elif hit_ewo:
                                    reason = "🔄 EWO+"
                                elif trail_armed and hit_sl:
                                    reason = "🔄 TS"
                                elif be_armed and hit_sl:
                                    reason = "🛡️ BE"
                                else:
                                    reason = "🛑 SL"

                                avail = get_asset_free_balance(base_asset)
                                sell_qty = min(pos["qty"], avail)
                                if float(format_quantity(sym, sell_qty)) <= 0:
                                    database.delete_active_trade(pos["id"])
                                    continue

                                if exec_type == "CHASE_LIMIT":
                                    ok, res = execute_smart_chase_order(sym, "SELL", qty=sell_qty)
                                else:
                                    ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")

                                if ok:
                                    entry_fee = position_entry_fee_rate(pos, exec_type)
                                    real_exit, sold_qty, gross_pnl, fee_usd, net_pnl = settle_exit_pnl(
                                        entry, sell_qty, res, bid, entry_fee, exec_type
                                    )
                                    bot_state["daily_pnl"] += net_pnl
                                    bot_state["daily_pnl_coins"][sym] = bot_state["daily_pnl_coins"].get(sym, 0.0) + net_pnl
                                    bot_state["trades_count"] += 1
                                    if net_pnl > 0:
                                        bot_state["winning_count"] += 1
                                    database.archive_closed_trade({
                                        "id": pos["id"], "bot_name": bKey, "symbol": sym,
                                        "entry_price": entry, "exit_price": real_exit,
                                        "qty": sold_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                        "net_pnl": net_pnl, "reason": f"{reason} [{MTF_TF_LABELS.get(tf, tf)}]",
                                        "entry_time": pos["time"], "exit_time": get_current_iso_time()
                                    })
                                    database.delete_active_trade(pos["id"])
                                    add_log(f"💰 [{bKey}][{MTF_TF_LABELS.get(tf, tf)}] إغلاق {sym} | خروج: {real_exit}$ | صافي: {net_pnl:+.3f}$ ({reason})", "sells", "success" if net_pnl > 0 else "danger")
                                    open_count = max(0, open_count - 1)
                                    used_cap = max(0.0, used_cap - position_notional(pos))
                                else:
                                    if "30005" in str(res) or "Oversold" in str(res):
                                        push_ops_alert("oversold", f"{bKey}: رصيد غير كافٍ لإغلاق {sym} (Oversold) — حُذفت من التتبع")
                                        database.delete_active_trade(pos["id"])
                                    else:
                                        still_sym.append(pos)
                            else:
                                still_sym.append(pos)

                        bot_state["active_positions"][sym] = still_sym

                        # Entries per enabled timeframe
                        if cfg.get("status") != "RUNNING":
                            continue
                        entries_ok, _lock = bot_entries_allowed(bot_state)
                        if not entries_ok:
                            continue

                        for tf in MTF_TF_ORDER:
                            tf_cfg = mtf.get(tf) or {}
                            if not tf_cfg.get("enabled"):
                                continue
                            size = float(tf_cfg.get("trade_size_usdt", 15.0))
                            if size <= 0:
                                continue

                            same_tf_count = sum(1 for p in bot_state["active_positions"].get(sym, []) if (p.get("timeframe") or (p.get("meta") or {}).get("timeframe")) == tf)
                            coin_count = len(bot_state["active_positions"].get(sym, []))
                            if same_tf_count > 0:
                                continue
                            if coin_count >= max_con:
                                continue
                            if open_count >= max_open:
                                break
                            if (used_cap + size) > max_alloc:
                                continue
                            if shared_state["real_balance_usdt"] < size:
                                push_ops_alert("balance", f"{bKey}: رصيد USDT غير كافٍ لشراء {sym}/{tf} (مطلوب {size}$)", cooldown_sec=300)
                                continue

                            candles = fetch_klines(sym, interval=tf, limit=45)
                            if not candles:
                                continue
                            latest_candle_time = candles[-1]["time"]
                            lock_key = f"{bKey}_{sym}_{tf}"
                            if LAST_ENTRY_CANDLE.get(lock_key) == latest_candle_time:
                                continue

                            ok_sig, _ewo = ewo_rebound_signal(candles)
                            if not ok_sig:
                                continue

                            if hierarchical and not mtf_hierarchy_ok(sym, tf, mtf):
                                continue

                            q = float(format_quantity(sym, size / ask))
                            if q <= 0:
                                continue
                            if exec_type == "CHASE_LIMIT":
                                ok, res = execute_smart_chase_order(sym, "BUY", quote_qty=size)
                            else:
                                ok, res = place_order(sym, "BUY", qty=q, quote_qty=size, order_type="MARKET")
                            if not ok:
                                continue

                            LAST_ENTRY_CANDLE[lock_key] = latest_candle_time
                            id_pfx = "bot_ewo_mtfh" if hierarchical else "bot_ewo_mtf"
                            trade_id = f"{id_pfx}_{tf}_{int(time.time()*1000)}"
                            time_str = get_current_iso_time()
                            fill_entry, fill_qty, entry_fee = build_entry_from_fill(res, ask, q, exec_type)
                            tp_pct = float(tf_cfg.get("tp_pct", 0.022))
                            sl_pct = float(tf_cfg.get("sl_pct", 0.010))
                            hier_parent = database.normalize_hierarchy_parent(tf, tf_cfg.get("hierarchy_parent", "off")) if hierarchical else "off"
                            meta = {
                                "timeframe": tf,
                                "trade_size_usdt": size,
                                "tp_pct": tp_pct,
                                "sl_pct": sl_pct,
                                "be_enabled": bool(tf_cfg.get("be_enabled", False)),
                                "be_trigger_pct": float(tf_cfg.get("be_trigger_pct", 0.012)),
                                "trail_enabled": bool(tf_cfg.get("trail_enabled", False)),
                                "trail_trigger_pct": float(tf_cfg.get("trail_trigger_pct", 0.018)),
                                "trail_cb_pct": float(tf_cfg.get("trail_cb_pct", 0.006)),
                                "be_armed": False,
                                "trail_armed": False,
                                "hierarchical": hierarchical,
                                "hierarchy_parent": hier_parent,
                                "entry_fee_rate": entry_fee
                            }
                            t_obj = {
                                "id": trade_id, "bot_name": bKey, "symbol": sym,
                                "entry_price": fill_entry, "highest_price": fill_entry, "qty": fill_qty,
                                "tp_pct": tp_pct, "sl_pct": sl_pct, "time_str": time_str,
                                "timeframe": tf, "meta": meta
                            }
                            database.insert_active_trade(t_obj)
                            bot_state["active_positions"].setdefault(sym, []).append({
                                "id": trade_id, "entry_price": fill_entry, "highest_price": fill_entry, "qty": fill_qty,
                                "tp_pct": tp_pct, "sl_pct": sl_pct, "time": time_str,
                                "timeframe": tf, "meta": meta, "entry_fee_rate": entry_fee,
                                "be_armed": False, "trail_armed": False
                            })
                            open_count += 1
                            used_cap += size
                            mode_tag = ""
                            if hierarchical:
                                parent_lab = "off" if hier_parent == "off" else MTF_TF_LABELS.get(hier_parent, hier_parent)
                                mode_tag = f" H→{parent_lab}"
                            add_log(f"📐 [{bKey}][{MTF_TF_LABELS.get(tf, tf)}]{mode_tag} شراء {sym} عند {fill_entry}$ بحجم {size}$ ({exec_type})", "buys", "primary")

        except Exception as e:
            add_log(f"خطأ محرك التداول: {e}", "system", "warning")

        time.sleep(7)

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>دخول</title>
<style>
body{background:#090d16;color:#fff;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#111827;padding:24px;border-radius:12px;width:300px;border:1px solid #1f293d}
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
        
        elif self.path == '/api/get_global_settings':
            settings = database.get_global_settings()
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(settings, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/get_sniper_profiles':
            profiles = database.get_sniper_profiles()
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(profiles, ensure_ascii=False).encode('utf-8'))

        # مسار سكانر الإشارات الفنية الاستباقية مع دعم دمج الفيوتشر والسبوت معاً
        elif self.path.startswith('/api/smart_scanner'):
            try:
                query = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(query)
                source = params.get("source", ["FUTURES"])[0]
                tf = params.get("tf", ["5m"])[0]
                filter_mode = params.get("filter", ["all"])[0]
                limit = int(params.get("limit", [4])[0])
                vol_th = float(params.get("vol_mult", [2.0])[0])
                rsi_th = float(params.get("rsi_th", [38.0])[0])

                top_candidates = []
                
                # جلب مرشحي الفيوتشر
                if source in ["FUTURES", "ALL"]:
                    try:
                        fut_url = f"{FUTURES_URL}/api/v1/contract/ticker"
                        req_f = urllib.request.Request(fut_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req_f, context=ssl_ctx, timeout=5) as res:
                            f_data = json.loads(res.read().decode('utf-8'))
                            if f_data.get("success") and "data" in f_data:
                                f_list = [
                                    {"symbol": t.get("symbol"), "quoteVolume": float(t.get("amount24", 0)), "source": "FUTURES"}
                                    for t in f_data["data"]
                                    if t.get("symbol", "").endswith("_USDT") and float(t.get("amount24", 0)) >= 50000
                                ]
                                f_list.sort(key=lambda x: x["quoteVolume"], reverse=True)
                                top_candidates.extend(f_list[:25])
                    except Exception:
                        pass

                # جلب مرشحي السبوت
                if source in ["SPOT", "ALL"]:
                    try:
                        url = f"{BASE_URL}/api/v3/ticker/24hr"
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
                            tickers = json.loads(res.read().decode('utf-8'))
                            usdt_tickers = [
                                {"symbol": t.get("symbol"), "quoteVolume": float(t.get("quoteVolume", 0)), "source": "SPOT"}
                                for t in tickers 
                                if t.get("symbol", "").endswith("USDT") and float(t.get("quoteVolume", 0)) >= 40000
                            ]
                            usdt_tickers.sort(key=lambda x: x["quoteVolume"], reverse=True)
                            top_candidates.extend(usdt_tickers[:25])
                    except Exception:
                        pass

                matched_signals = []
                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(executor.map(lambda t: evaluate_coin_signals(t, source=t.get("source", "SPOT"), tf=tf, vol_th=vol_th, rsi_th=rsi_th), top_candidates))
                    seen_symbols = set()
                    for r in results:
                        if r and r["symbol"] not in seen_symbols:
                            seen_symbols.add(r["symbol"])
                            if filter_mode == "triple" and r["signals_count"] < 3: continue
                            if filter_mode == "vol" and not r["sig_vol"]: continue
                            if filter_mode == "rsi" and not r["sig_rsi"]: continue
                            if filter_mode == "ewo" and not r["sig_ewo"]: continue
                            matched_signals.append(r)

                matched_signals.sort(key=lambda x: (x["signals_count"], float(x["change_d_pct"])), reverse=True)
                
                self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(json.dumps(matched_signals[:limit], ensure_ascii=False).encode('utf-8'))
            except Exception:
                self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
                self.wfile.write(b"[]")

        elif self.path == '/api/sniper_data':
            res_data = {
                "positions": shared_state.get("sniper_positions", []),
                "market_prices": shared_state.get("market_prices", {})
            }
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(res_data, ensure_ascii=False).encode('utf-8'))

        elif self.path.startswith('/api/exchange_orders'):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            sym = params.get("symbol", ["SOLUSDT"])[0]
            ok, ords = mexc_private_request("/api/v3/allOrders", params={"symbol": sym, "limit": 40})
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(ords if ok else [], ensure_ascii=False).encode('utf-8'))

        elif self.path.startswith('/api/history'):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            bot_name = params.get("bot_name", [None])[0]
            try:
                limit = int(params.get("limit", [70])[0])
            except Exception:
                limit = 70
            limit = max(1, min(limit, 2000))
            trades = database.get_closed_trades(bot_name, limit=limit)
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(trades, ensure_ascii=False).encode('utf-8'))

        elif self.path.startswith('/api/export_closed'):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            bot_name = params.get("bot_name", [None])[0]
            trades = database.get_closed_trades(bot_name, limit=2000)
            lines = ["id,bot_name,symbol,entry_price,exit_price,qty,gross_pnl,fee_usd,net_pnl,reason,entry_time,exit_time"]
            for t in trades:
                lines.append(",".join([
                    str(t.get("id", "")),
                    str(t.get("bot_name", "")),
                    str(t.get("symbol", "")),
                    str(t.get("entry_price", "")),
                    str(t.get("exit_price", "")),
                    str(t.get("qty", "")),
                    str(t.get("gross_pnl", "")),
                    str(t.get("fee_usd", "")),
                    str(t.get("net_pnl", "")),
                    '"' + str(t.get("reason", "")).replace('"', "'") + '"',
                    str(t.get("entry_time", "")),
                    str(t.get("exit_time", ""))
                ]))
            csv_body = "\n".join(lines)
            fname = f"closed_{(bot_name or 'ALL')}.csv"
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
            self.end_headers()
            self.wfile.write(csv_body.encode('utf-8'))

        elif self.path.startswith('/api/export_logs'):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            bot_name = (params.get("bot_name", [""])[0] or "").strip()
            lines = []
            for l in shared_state.get("recent_logs", []):
                msg = str(l.get("msg", ""))
                if bot_name and bot_name not in msg and bot_name.replace("_", " ") not in msg:
                    # also allow BOT_X / bot_x case variants
                    if bot_name.lower() not in msg.lower():
                        continue
                lines.append(f"[{l.get('time','')}] ({l.get('cat','')}/{l.get('type','')}) {msg}")
            body = "\n".join(lines) if lines else "لا توجد سجلات مطابقة"
            fname = f"logs_{(bot_name or 'ALL')}.txt"
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
            self.end_headers()
            self.wfile.write(body.encode('utf-8'))

        elif self.path == '/sniper':
            try:
                with open("sniper.html", "r", encoding="utf-8") as f:
                    html_c = f.read()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(html_c.encode('utf-8'))
            except Exception:
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(b"<h3>sniper.html not found.</h3><a href='/'>Back</a>")

        elif self.path == '/analytics':
            try:
                with open("analytics.html", "r", encoding="utf-8") as f:
                    html_c = f.read()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(html_c.encode('utf-8'))
            except Exception:
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(b"<h3>analytics.html not found.</h3><a href='/'>Back</a>")

        elif self.path == '/api/logout':
            self.send_response(200); self.send_header('Set-Cookie', 'session_id=; Path=/; Max-Age=0'); self.end_headers()

        else:
            try:
                with open("dashboard.html", "r", encoding="utf-8") as f:
                    html_c = f.read()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(html_c.encode('utf-8'))
            except Exception:
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(b"<h3>dashboard.html not found.</h3>")

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

        if self.path == '/api/save_global_settings':
            timeout = data.get("chase_timeout", 12)
            interval = data.get("chase_interval", 2.0)
            database.save_global_settings(timeout, interval)
            add_log(f"⚙️ تم تحديث إعدادات التتبع: مهلة {timeout}s | فاصل {interval}s", "system", "info")
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم حفظ الإعدادات بنجاح!"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/save_sniper_profile':
            p_id = data.get("profile_id", "SNIPER_1")
            updates = {
                "trade_size": float(data.get("trade_size", 10.0)),
                "order_type": data.get("order_type", "CHASE_LIMIT"),
                "tp1_pct": float(data.get("tp1_pct", 0.015)),
                "tp2_pct": float(data.get("tp2_pct", 0.030)),
                "sl_pct": float(data.get("sl_pct", 0.010)),
                "trailing_cb": float(data.get("trailing_cb", 0.006))
            }
            database.save_sniper_profile(p_id, updates)
            add_log(f"💾 تم حفظ بروفايل {p_id} في SQLite", "system", "info")
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم حفظ البروفايل بنجاح!"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/sniper_buy':
            prof = data.get("sniper_profile", "SNIPER_1")
            sym = data.get("symbol")
            size = float(data.get("size", 10.0))
            o_type = data.get("order_type", "CHASE_LIMIT")
            tp1_pct = float(data.get("tp1_pct", 0.015))
            tp2_pct = float(data.get("tp2_pct", 0.030))
            sl_pct = float(data.get("sl_pct", 0.010))
            ts_cb = float(data.get("trailing_cb", 0.006))

            bid, ask = get_orderbook(sym)
            if ask:
                q = float(format_quantity(sym, size / ask))
                if o_type == "CHASE_LIMIT":
                    ok, res = execute_smart_chase_order(sym, "BUY", quote_qty=size)
                else:
                    ok, res = place_order(sym, "BUY", qty=q, quote_qty=size, order_type="MARKET")

                if ok:
                    fill_entry, fill_qty, entry_fee = build_entry_from_fill(res, ask, q, o_type)
                    snp_id = f"snp_{int(time.time()*1000)}"
                    time_str = get_current_iso_time()
                    snp_trade = {
                        "id": snp_id, "sniper_profile": prof, "symbol": sym, "entry_price": fill_entry,
                        "highest_price": fill_entry, "qty": fill_qty, "orig_qty": fill_qty,
                        "tp1_pct": tp1_pct, "tp2_pct": tp2_pct,
                        "sl_pct": sl_pct, "trailing_cb": ts_cb,
                        "tp1_hit": 0, "time_str": time_str,
                        "entry_fee_rate": entry_fee
                    }
                    database.insert_sniper_trade(snp_trade)
                    shared_state["sniper_positions"].append(snp_trade)
                    msg = f"🎯 تم إطلاق {prof} لـ {sym} عند {fill_entry}$ (كمية: {fill_qty})"
                    add_log(msg, "buys", "primary")
                else:
                    msg = f"❌ فشل القنص: {res}"
            else:
                msg = "تعذر قراءة سعر العملة"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/sniper_close':
            s_id = data.get("id")
            sym = data.get("symbol")
            bid, ask = get_orderbook(sym)
            base_asset = sym.replace("USDT", "").replace("USDC", "")
            for sp in shared_state.get("sniper_positions", []):
                if sp["id"] == s_id:
                    avail = get_asset_free_balance(base_asset)
                    sell_qty = min(sp["qty"], avail)
                    if float(format_quantity(sym, sell_qty)) > 0:
                        ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")
                        if ok:
                            entry_fee = position_entry_fee_rate(sp, "MARKET")
                            real_exit, sold_qty, gross_pnl, fee_usd, net_pnl = settle_exit_pnl(
                                sp["entry_price"], sell_qty, res, bid, entry_fee, "MARKET"
                            )
                        
                            database.archive_closed_trade({
                                "id": s_id, "bot_name": sp.get("sniper_profile", "SNIPER_1"), "symbol": sym,
                                "entry_price": sp["entry_price"], "exit_price": real_exit,
                                "qty": sold_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                "net_pnl": net_pnl, "reason": "إغلاق يدوي للقناص",
                                "entry_time": sp["time_str"], "exit_time": get_current_iso_time()
                            })
                    database.delete_sniper_trade(s_id)
                    add_log(f"🔥 تسييل صفقة قناص {sym} يدوي", "sells", "danger")
                    break
            shared_state["sniper_positions"] = [p for p in shared_state.get("sniper_positions", []) if p["id"] != s_id]
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم إغلاق وتسييل صفقة القناص"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/sniper_edit':
            s_id = data.get("id")
            updates = {
                "entry_price": float(data.get("entry_price", 0.0)),
                "qty": float(data.get("qty", 0.0)),
                "tp1_pct": float(data.get("tp1_pct", 0.015)),
                "sl_pct": float(data.get("sl_pct", 0.010))
            }
            database.update_sniper_trade(s_id, updates)
            for sp in shared_state.get("sniper_positions", []):
                if sp["id"] == s_id:
                    sp.update(updates)
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم حفظ التعديل"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/sniper_unlink':
            s_id = data.get("id")
            database.delete_sniper_trade(s_id)
            shared_state["sniper_positions"] = [p for p in shared_state.get("sniper_positions", []) if p["id"] != s_id]
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم فك الربط"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/change_password':
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
            st = data.get("status", "PAUSED")
            database.update_bot_config(b_name, {"status": st})
            if b_name in shared_state["bots"]:
                shared_state["bots"][b_name]["status"] = st
            add_log(f"تغيير حالة {b_name} إلى: {st}", "system", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/save_bot_config':
            b_name = data.pop("bot_name", "BOT_1")
            before = database.get_bot_config(b_name) or {}
            # Normalize MTF hierarchy parents before save
            if isinstance(data.get("mtf_settings"), dict):
                data["mtf_settings"] = database.parse_mtf_settings(data.get("mtf_settings"))
            database.update_bot_config(b_name, data)
            after = database.get_bot_config(b_name) or {}
            # Prefer request payload values for change summary when present
            merged_after = dict(after)
            merged_after.update(data)
            if "mtf_settings" in data:
                merged_after["mtf_settings"] = data["mtf_settings"]
            for line in summarize_bot_config_changes(b_name, before, merged_after):
                add_log(line, "system", "info")
            self.send_response(200); self.end_headers()

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
                    add_log(f"➕ إضافة {raw_sym} إلى {b_name}", "system", "success")
                    msg = f"✅ تمت إضافة {raw_sym} بنجاح!"
                else:
                    msg = "العملة موجودة بالفعل"
            else:
                msg = "رمز العملة غير صالح"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/remove_symbol':
            b_name = data.get("bot_name", "BOT_1")
            sym = data.get("symbol", "").strip().upper()
            cfg = database.get_bot_config(b_name)
            current_syms = parse_symbols_list(cfg.get("symbols", ""))
            if sym in current_syms:
                current_syms.remove(sym)
                database.update_bot_config(b_name, {"symbols": ", ".join(current_syms)})
                add_log(f"🗑️ إزالة {sym} من {b_name}", "system", "warning")
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
            exec_type = cfg.get("order_exec_type", "CHASE_LIMIT")
            bid, ask = get_orderbook(sym)
            if ask:
                q = float(format_quantity(sym, size / ask))
                if exec_type == "CHASE_LIMIT":
                    ok, res = execute_smart_chase_order(sym, "BUY", quote_qty=size)
                else:
                    ok, res = place_order(sym, "BUY", qty=q, quote_qty=size, order_type="MARKET")
                
                if ok:
                    trade_id = f"{b_name.lower()}_{int(time.time()*1000)}"
                    time_str = get_current_iso_time()
                    fill_entry, fill_qty, entry_fee = build_entry_from_fill(res, ask, q, exec_type)
                    t_obj = {
                        'id': trade_id, 'bot_name': b_name, 'symbol': sym,
                        'entry_price': fill_entry, 'highest_price': fill_entry, 'qty': fill_qty,
                        'tp_pct': float(cfg.get("tp_pct", 0.025)),
                        'sl_pct': float(cfg.get("sl_pct", 0.012)),
                        'time_str': time_str,
                        'meta': {'entry_fee_rate': entry_fee}
                    }
                    database.insert_active_trade(t_obj)
                    if sym not in shared_state["bots"][b_name]["active_positions"]:
                        shared_state["bots"][b_name]["active_positions"][sym] = []
                    shared_state["bots"][b_name]["active_positions"][sym].append({
                        'id': trade_id, 'entry_price': fill_entry, 'highest_price': fill_entry, 'qty': fill_qty,
                        'tp_pct': t_obj['tp_pct'], 'sl_pct': t_obj['sl_pct'], 'time': time_str,
                        'entry_fee_rate': entry_fee, 'meta': {'entry_fee_rate': entry_fee}
                    })
                    msg = f"✅ تم شراء {sym} عبر {b_name} عند {fill_entry}$ ({exec_type})"
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
            base_asset = sym.replace("USDT", "").replace("USDC", "")

            new_positions = []
            found = False
            for p in shared_state["bots"][b_name]["active_positions"].get(sym, []):
                if p.get("id") == pos_id and not found:
                    found = True
                    avail = get_asset_free_balance(base_asset)
                    sell_qty = min(p['qty'], avail)

                    if float(format_quantity(sym, sell_qty)) > 0:
                        cfg = database.get_bot_config(b_name)
                        exec_type = cfg.get("order_exec_type", "CHASE_LIMIT")
                        if exec_type == "CHASE_LIMIT":
                            ok, res = execute_smart_chase_order(sym, "SELL", qty=sell_qty)
                        else:
                            ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")
                        if ok:
                            entry_fee = position_entry_fee_rate(p, exec_type)
                            real_exit, sold_qty, gross_pnl, fee_usd, net_pnl = settle_exit_pnl(
                                p['entry_price'], sell_qty, res, bid, entry_fee, exec_type
                            )

                            shared_state["bots"][b_name]["daily_pnl"] += net_pnl
                            shared_state["bots"][b_name]["daily_pnl_coins"][sym] = shared_state["bots"][b_name]["daily_pnl_coins"].get(sym, 0.0) + net_pnl
                            shared_state["bots"][b_name]["trades_count"] += 1
                            if net_pnl > 0: shared_state["bots"][b_name]["winning_count"] += 1
                            
                            database.archive_closed_trade({
                                "id": pos_id, "bot_name": b_name, "symbol": sym,
                                "entry_price": p["entry_price"], "exit_price": real_exit,
                                "qty": sold_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                "net_pnl": net_pnl, "reason": "يدوي (Manual)",
                                "entry_time": p["time"], "exit_time": get_current_iso_time()
                            })
                            database.delete_active_trade(pos_id)
                            add_log(f"🔥 تسييل {sym} في {b_name} بسعر {real_exit}$ | صافي: {net_pnl:+.3f}$", "sells", "danger")
                        else:
                            new_positions.append(p)
                            continue
                    else:
                        database.delete_active_trade(pos_id)
                        add_log(f"⚠️ الرصيد 0، حذفت الصفقة", "system", "warning")
                else:
                    new_positions.append(p)
            shared_state["bots"][b_name]["active_positions"][sym] = new_positions
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم تسييل الصفقة وأرشفتها"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/unlink_position':
            sym = data.get("symbol")
            pos_id = data.get("pos_id")
            b_name = data.get("bot_name", "BOT_1")
            database.delete_active_trade(pos_id)
            shared_state["bots"][b_name]["active_positions"][sym] = [p for p in shared_state["bots"][b_name]["active_positions"].get(sym, []) if p.get("id") != pos_id]
            add_log(f"🚫 تم فك ربط صفقة {sym} من {b_name} دون بيعها", "system", "info")
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم فك الربط بنجاح"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/edit_position':
            pos_id = data.get("pos_id")
            updates = {
                "entry_price": float(data.get("entry_price", 0.0)),
                "qty": float(data.get("qty", 0.0)),
                "tp_pct": float(data.get("tp_pct", 0.025)),
                "sl_pct": float(data.get("sl_pct", 0.012))
            }
            database.update_active_trade(pos_id, updates)
            for bKey in BOT_KEYS:
                for s in shared_state["bots"][bKey]["active_positions"]:
                    for p in shared_state["bots"][bKey]["active_positions"][s]:
                        if p.get("id") == pos_id:
                            p.update(updates)
            add_log(f"✏️ تم تعديل الصفقة {pos_id} في SQLite", "system", "success")
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم التعديل بنجاح"}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/panic_custom':
            asset = data.get("asset")
            mode = data.get("mode", "UNLINKED")
            free_qty = get_asset_free_balance(asset)
            bot_alloc = get_total_bot_allocated_qty(asset)
            sym = f"{asset}USDT"

            if mode == "UNLINKED":
                sell_qty = max(0.0, free_qty - bot_alloc)
                if sell_qty > 0:
                    ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")
                    msg = f"✅ تم تسييل الفائض الحر ({sell_qty} {asset})" if ok else f"❌ فشل: {res}"
                else:
                    msg = "لا يوجد رصيد حر فائض للبيع"
            else:
                if free_qty > 0:
                    ok, res = place_order(sym, "SELL", qty=free_qty, order_type="MARKET")
                    if ok:
                        for bKey in BOT_KEYS:
                            for p in shared_state["bots"][bKey]["active_positions"].get(sym, []):
                                database.delete_active_trade(p["id"])
                            shared_state["bots"][bKey]["active_positions"][sym] = []
                        for sp in shared_state.get("sniper_positions", []):
                            if sp["symbol"] == sym:
                                database.delete_sniper_trade(sp["id"])
                        shared_state["sniper_positions"] = [sp for sp in shared_state.get("sniper_positions", []) if sp["symbol"] != sym]
                        msg = f"✅ تم تسييل كامل الرصيد ({free_qty} {asset}) وتصفير الصفقات"
                    else:
                        msg = f"❌ فشل: {res}"
                else:
                    msg = "لا يوجد رصيد متاح"

            add_log(msg, "sells", "danger")
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/cancel_order':
            sym = data.get("symbol")
            order_id = data.get("order_id")
            ok, res = mexc_private_request("/api/v3/order", method="DELETE", params={"symbol": sym, "orderId": order_id})
            msg = f"✅ تم إلغاء الأمر {order_id}" if ok else f"❌ فشل الإلغاء: {res}"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/terminal_trade':
            sym = data.get("symbol")
            side = data.get("side")
            o_type = data.get("order_type")
            val = float(data.get("val", 0.0))
            price = float(data.get("price", 0.0)) if data.get("price") else None
            
            if o_type == "CHASE_LIMIT":
                ok, res = execute_smart_chase_order(sym, side, quote_qty=val if side=="BUY" else None, qty=val if side=="SELL" else None)
            elif side == "BUY" and o_type == "MARKET":
                ok, res = place_order(sym, side, quote_qty=val, order_type=o_type)
            elif o_type == "LIMIT":
                ok, res = place_order(sym, side, qty=val, price=price, order_type=o_type)
            else:
                ok, res = place_order(sym, side, qty=val, order_type=o_type)
            
            msg = f"✅ تم تنفيذ أمر {side} لـ {sym}" if ok else f"❌ فشل: {res}"
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/convert_dust_direct':
            total_sold_usd = 0.0
            sold_n = 0
            skip_min = 0
            skip_unsup = 0
            skip_other = 0
            for a in shared_state.get("wallet_assets", []):
                asset = a.get("asset")
                if asset in ("USDT", "USDC", "MX"):
                    continue
                free_qty = float(a.get("free", 0.0) or 0.0)
                val_usd = float(a.get("usd_value", 0.0) or 0.0)
                if free_qty <= 0:
                    continue
                # Dust window: sellable only if >= exchange min notional and < 5$
                if val_usd >= 5.0:
                    continue
                ok_can, info = can_market_sell_wallet_asset(a, min_notional=MIN_SELL_NOTIONAL_USDT)
                if not ok_can:
                    if info == "below_min":
                        skip_min += 1
                    elif info == "unsupported":
                        skip_unsup += 1
                    else:
                        skip_other += 1
                    continue
                sym = info
                ok, res = place_order(sym, "SELL", qty=free_qty, order_type="MARKET")
                if ok:
                    total_sold_usd += val_usd
                    sold_n += 1
                else:
                    err_s = str(res)
                    if "10007" in err_s or "not support api" in err_s.lower():
                        skip_unsup += 1
                    elif "30002" in err_s:
                        skip_min += 1
                    else:
                        skip_other += 1
            if total_sold_usd > 1.0:
                place_order("MXUSDT", "BUY", quote_qty=total_sold_usd, order_type="MARKET")
                msg = f"✅ تحويل غبار→MX: بيع {sold_n} بقيمة {total_sold_usd:.2f}$"
            else:
                msg = "لا توجد أرصدة صغيرة قابلة للتحويل (≥1$ و <5$)"
            skip_bits = []
            if skip_min:
                skip_bits.append(f"{skip_min} دون حد 1$")
            if skip_unsup:
                skip_bits.append(f"{skip_unsup} غير مدعوم API")
            if skip_other:
                skip_bits.append(f"{skip_other} أخرى")
            if skip_bits:
                msg += " | تخطي: " + "، ".join(skip_bits)
            add_log(msg, "system", "info")
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": msg}, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/panic_all':
            sold_count = 0
            skip_min = 0
            skip_unsup = 0
            skip_other = 0
            for a in shared_state.get("wallet_assets", []):
                asset = a.get("asset")
                free_qty = float(a.get("free", 0.0) or 0.0)
                if asset == "USDT" or free_qty <= 0:
                    continue
                ok_can, info = can_market_sell_wallet_asset(a, min_notional=MIN_SELL_NOTIONAL_USDT)
                if not ok_can:
                    if info == "below_min":
                        skip_min += 1
                    elif info == "unsupported":
                        skip_unsup += 1
                        add_log(f"⚠️ تخطي {asset}: غير مدعوم عبر API", "system", "warning")
                    else:
                        skip_other += 1
                    continue
                sym = info
                ok, res = place_order(sym, "SELL", qty=free_qty, order_type="MARKET")
                if ok:
                    sold_count += 1
                    for bKey in BOT_KEYS:
                        for p in shared_state["bots"][bKey]["active_positions"].get(sym, []):
                            database.delete_active_trade(p["id"])
                        shared_state["bots"][bKey]["active_positions"][sym] = []
                    for sp in shared_state.get("sniper_positions", []):
                        if sp["symbol"] == sym:
                            database.delete_sniper_trade(sp["id"])
                    shared_state["sniper_positions"] = [sp for sp in shared_state.get("sniper_positions", []) if sp["symbol"] != sym]
                else:
                    err_s = str(res)
                    if "10007" in err_s or "not support api" in err_s.lower():
                        skip_unsup += 1
                    elif "30002" in err_s:
                        skip_min += 1
                    else:
                        skip_other += 1
            msg = f"✅ تم تسييل {sold_count} عملات إلى USDT وتصفير كافة الصفقات" if sold_count > 0 else "لا توجد عملات قابلة للتسييل (≥1$ ومدعومة API)"
            skip_bits = []
            if skip_min:
                skip_bits.append(f"{skip_min} دون حد 1$")
            if skip_unsup:
                skip_bits.append(f"{skip_unsup} غير مدعوم API")
            if skip_other:
                skip_bits.append(f"{skip_other} أخرى")
            if skip_bits:
                msg += " | تخطي: " + "، ".join(skip_bits)
            add_log(msg, "system", "info")
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
