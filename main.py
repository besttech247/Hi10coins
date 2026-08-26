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
LAST_ENTRY_CANDLE = {}
BOT_KEYS = ["BOT_1", "BOT_2A", "BOT_2B", "BOT_2C", "BOT_3"]

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
    "open_limit_orders": [],
    "start_timestamp": START_TIME,
    "current_day": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    "bots": {}
}

for k in BOT_KEYS:
    shared_state["bots"][k] = {
        "name": k,
        "status": "PAUSED",
        "symbols": [],
        "order_exec_type": "CHASE_LIMIT" if k != "BOT_3" else "MARKET",
        "max_allocation": 50.0,
        "max_concurrent": 1,
        "trade_size": 10.0,
        "tp_pct": 2.5,
        "sl_pct": 1.2,
        "timeframe": "15m",
        "daily_pnl": 0.0,
        "daily_target": 5.0,
        "trades_count": 0,
        "winning_count": 0,
        "daily_pnl_coins": {},
        "active_positions": {}
    }

def init_trades_from_db():
    try:
        rows = database.load_all_active_trades()
        for r in rows:
            bKey = r.get("bot_name")
            sym = r.get("symbol")
            if bKey in shared_state["bots"]:
                if sym not in shared_state["bots"][bKey]["active_positions"]:
                    shared_state["bots"][bKey]["active_positions"][sym] = []
                shared_state["bots"][bKey]["active_positions"][sym].append({
                    "id": r.get("id"),
                    "entry_price": float(r.get("entry_price", 0.0)),
                    "highest_price": float(r.get("highest_price", r.get("entry_price", 0.0))),
                    "qty": float(r.get("qty", 0.0)),
                    "tp_pct": float(r.get("tp_pct", 0.025)),
                    "sl_pct": float(r.get("sl_pct", 0.012)),
                    "time": r.get("time_str", "--:--")
                })
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

def fetch_klines(symbol, interval="15m", limit=45):
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=6) as res:
            data = json.loads(res.read().decode('utf-8'))
            return [{'time': int(r[0]), 'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4])} for r in data]
    except Exception:
        return []

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
    
    add_log(f"📤 طلب {side.upper()} {symbol} ({order_type})", "orders", "info")
    ok, res = mexc_private_request("/api/v3/order", method="POST", params=params)
    if not ok:
        add_log(f"❌ خطأ {symbol}: {res}", "orders", "danger")
    return ok, res

def execute_smart_chase_order(symbol, side, qty=None, quote_qty=None, max_chase_secs=12):
    bid, ask = get_orderbook(symbol)
    if not bid or not ask:
        return place_order(symbol, side, qty=qty, quote_qty=quote_qty, order_type="MARKET")
    
    order_price = bid if side.upper() == "BUY" else ask
    order_qty = qty if qty else (quote_qty / order_price if quote_qty else 0)
    
    ok, res = place_order(symbol, side, qty=order_qty, price=order_price, order_type="LIMIT")
    if not ok:
        return place_order(symbol, side, qty=qty, quote_qty=quote_qty, order_type="MARKET")
    
    order_id = res.get("orderId")
    start_t = time.time()
    
    while time.time() - start_t < max_chase_secs:
        time.sleep(2.5)
        cur_bid, cur_ask = get_orderbook(symbol)
        best_price = cur_bid if side.upper() == "BUY" else cur_ask
        
        ok_chk, ord_info = mexc_private_request("/api/v3/order", params={"symbol": symbol, "orderId": order_id})
        if ok_chk and ord_info.get("status") == "FILLED":
            return True, ord_info
        
        if best_price != order_price:
            mexc_private_request("/api/v3/order", method="DELETE", params={"symbol": symbol, "orderId": order_id})
            order_price = best_price
            ok_re, res_re = place_order(symbol, side, qty=order_qty, price=order_price, order_type="LIMIT")
            if ok_re:
                order_id = res_re.get("orderId")
            else:
                break

    mexc_private_request("/api/v3/order", method="DELETE", params={"symbol": symbol, "orderId": order_id})
    return place_order(symbol, side, qty=qty, quote_qty=quote_qty, order_type="MARKET")

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

def refresh_wallet_and_prices():
    try:
        all_active_symbols = set()
        for bKey in BOT_KEYS:
            cfg = database.get_bot_config(bKey)
            syms = parse_symbols_list(cfg.get("symbols", ""))
            shared_state["bots"][bKey]["symbols"] = syms
            for s in syms: all_active_symbols.add(s)

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
                shared_state["bots"][bKey]["trailing_stop"] = int(cfg.get("trailing_stop", 0))

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

                    if bKey in ["BOT_1", "BOT_2A", "BOT_2B", "BOT_2C"]:
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
                                            gross_pnl = (bid - pos['entry_price']) * sell_qty
                                            fee_rate = 0.0 if exec_type == "CHASE_LIMIT" else 0.001
                                            fee_usd = (pos['entry_price'] * sell_qty * fee_rate) + (bid * sell_qty * fee_rate)
                                            net_pnl = gross_pnl - fee_usd
                                            
                                            shared_state["bots"][bKey]["daily_pnl"] += net_pnl
                                            shared_state["bots"][bKey]["daily_pnl_coins"][sym] += net_pnl
                                            shared_state["bots"][bKey]["trades_count"] += 1
                                            if net_pnl > 0: shared_state["bots"][bKey]["winning_count"] += 1
                                            
                                            database.archive_closed_trade({
                                                "id": pos["id"], "bot_name": bKey, "symbol": sym,
                                                "entry_price": pos["entry_price"], "exit_price": bid,
                                                "qty": sell_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                                "net_pnl": net_pnl, "reason": reason,
                                                "entry_time": pos["time"], "exit_time": datetime.now(timezone.utc).strftime("%H:%M")
                                            })
                                            add_log(f"💰 [{bKey}] بيع {sym} | صافي: {net_pnl:+.3f}$ ({reason})", "sells", "success" if net_pnl > 0 else "danger")
                                        else:
                                            if "30005" in str(res) or "Oversold" in str(res):
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
                                port_not_locked = shared_state["bots"][bKey]["daily_pnl"] < shared_state["bots"][bKey]["daily_target"]

                                if cfg.get("status") == "RUNNING" and sig_rebound and not is_candle_locked and can_open_coin and can_open_alloc and port_not_locked:
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
                                                time_str = datetime.now(timezone.utc).strftime("%H:%M")
                                                t_obj = {
                                                    'id': trade_id, 'bot_name': bKey, 'symbol': sym,
                                                    'entry_price': ask, 'highest_price': ask, 'qty': q,
                                                    'tp_pct': default_tp_pct, 'sl_pct': default_sl_pct,
                                                    'time_str': time_str
                                                }
                                                database.insert_active_trade(t_obj)
                                                shared_state["bots"][bKey]["active_positions"][sym].append({
                                                    'id': trade_id, 'entry_price': ask, 'highest_price': ask, 'qty': q,
                                                    'tp_pct': default_tp_pct, 'sl_pct': default_sl_pct, 'time': time_str
                                                })
                                                current_used_cap += size
                                                add_log(f"🚀 [{bKey}] شراء {sym} عند {ask}$ ({exec_type})", "buys", "primary")

                    elif bKey == "BOT_3":
                        default_tp_pct = float(cfg.get("tp_pct", 0.025))
                        default_sl_pct = float(cfg.get("sl_pct", 0.012))
                        use_ts = bool(cfg.get("trailing_stop", 1))
                        cb_pct = float(cfg.get("trailing_cb", 0.005))

                        still_b3 = []
                        for pos in shared_state["bots"]["BOT_3"]["active_positions"].get(sym, []):
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
                            trailing_sl = highest * (1.0 - cb_pct) if use_ts else sl_price
                            effective_sl = max(sl_price, trailing_sl) if use_ts and highest >= (entry * (1.0 + cb_pct)) else sl_price

                            hit_tp = bid >= tp_price
                            hit_sl = bid <= effective_sl

                            if hit_tp or hit_sl:
                                reason = "🎯 TP" if hit_tp else ("🔄 TS" if use_ts and effective_sl > sl_price else "🛑 SL")
                                avail = get_asset_free_balance(base_asset)
                                sell_qty = min(pos['qty'], avail)

                                if float(format_quantity(sym, sell_qty)) <= 0:
                                    database.delete_active_trade(pos['id'])
                                    continue

                                ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")
                                if ok:
                                    gross_pnl = (bid - entry) * sell_qty
                                    fee_usd = (entry * sell_qty * 0.001) + (bid * sell_qty * 0.001)
                                    net_pnl = gross_pnl - fee_usd

                                    shared_state["bots"]["BOT_3"]["daily_pnl"] += net_pnl
                                    shared_state["bots"]["BOT_3"]["daily_pnl_coins"][sym] += net_pnl
                                    shared_state["bots"]["BOT_3"]["trades_count"] += 1
                                    if net_pnl > 0: shared_state["bots"]["BOT_3"]["winning_count"] += 1
                                    
                                    database.archive_closed_trade({
                                        "id": pos["id"], "bot_name": "BOT_3", "symbol": sym,
                                        "entry_price": entry, "exit_price": bid,
                                        "qty": sell_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                        "net_pnl": net_pnl, "reason": reason,
                                        "entry_time": pos["time"], "exit_time": datetime.now(timezone.utc).strftime("%H:%M")
                                    })
                                    add_log(f"💰 [Bot 3] إغلاق {sym} | صافي: {net_pnl:+.3f}$ ({reason})", "sells", "success" if net_pnl > 0 else "danger")
                                else:
                                    if "30005" in str(res) or "Oversold" in str(res):
                                        database.delete_active_trade(pos['id'])
                                    else:
                                        still_b3.append(pos)
                            else:
                                still_b3.append(pos)
                        shared_state["bots"]["BOT_3"]["active_positions"][sym] = still_b3

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

ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>التداول اليدوي وسجل المنصة</title>
<style>
:root{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f3f4f6;--sub:#94a3b8}
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:10px;line-height:1.4}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px;margin-bottom:8px}
.btn{padding:6px 10px;border:none;border-radius:6px;font-weight:bold;cursor:pointer;font-size:11px}
input,select{background:#090d16;border:1px solid var(--border);color:#fff;padding:6px;border-radius:6px;font-size:11px;width:100%}
.pct-btn{background:#151e30;border:1px solid var(--border);color:var(--sub);padding:3px 6px;border-radius:4px;cursor:pointer;font-size:10px}
table{width:100%;border-collapse:collapse;text-align:right}
th,td{padding:5px 4px;border-bottom:1px solid var(--border);font-size:11px}
th{color:var(--sub)}
</style>
</head>
<body>
  <div class="card" style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <strong>📊 مركز التداول وسجل MEXC</strong>
      <div style="font-size:10px;color:var(--sub)">USDT المتاح: <strong id="m-usdt" style="color:#10b981">0.00 $</strong></div>
    </div>
    <a href="/" class="btn" style="background:#334155;color:#fff;text-decoration:none">🔙 الرئيسية</a>
  </div>

  <details open class="card">
    <summary style="font-weight:bold;cursor:pointer;color:#38bdf8;padding:4px">⚡ لوحة الشراء والبيع (Chase / Market / Limit) ▾</summary>
    <div style="padding-top:8px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px">
        <div>
          <label style="font-size:10px;color:var(--sub)">العملة:</label>
          <input type="text" id="t-sym" value="SOLUSDT">
        </div>
        <div>
          <label style="font-size:10px;color:var(--sub)">النوع:</label>
          <select id="t-type" onchange="document.getElementById('limit-price-box').style.display = this.value === 'LIMIT' ? 'block' : 'none'">
            <option value="CHASE_LIMIT" selected>متتبع ليميت (Chase 0% Fee)</option>
            <option value="MARKET">سعر السوق (Market)</option>
            <option value="LIMIT">أمر معلق (Limit)</option>
          </select>
        </div>
      </div>

      <div id="limit-price-box" style="display:none;margin-bottom:6px">
        <label style="font-size:10px;color:var(--sub)">سعر الليميت ($):</label>
        <input type="number" id="t-price" placeholder="أدخل السعر">
      </div>

      <div style="margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
          <label style="font-size:10px;color:var(--sub)">الكمية أو القيمة:</label>
          <div style="display:flex;gap:3px">
            <button class="pct-btn" onclick="setPct(0.25)">25%</button>
            <button class="pct-btn" onclick="setPct(0.50)">50%</button>
            <button class="pct-btn" onclick="setPct(0.75)">75%</button>
            <button class="pct-btn" onclick="setPct(1.00)">100%</button>
          </div>
        </div>
        <input type="number" id="t-qty" placeholder="القيمة أو الكمية">
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        <button class="btn" style="background:#10b981;color:#fff" onclick="execTrade('BUY')">🟢 شراء</button>
        <button class="btn" style="background:#ef4444;color:#fff" onclick="execTrade('SELL')">🔴 بيع</button>
      </div>
    </div>
  </details>

  <details open class="card">
    <summary style="font-weight:bold;cursor:pointer;color:#60a5fa;padding:4px">📜 سجل الأوامر الحقيقي المباشر من MEXC ▾</summary>
    <div style="display:flex;gap:4px;margin:6px 0">
      <input type="text" id="hist-sym" value="SOLUSDT" style="width:140px">
      <button class="btn" style="background:#0284c7;color:#fff" onclick="fetchExchangeHistory()">🔍 جلب السجل</button>
    </div>
    <div style="overflow-x:auto">
      <table id="mexc-history-table">
        <thead>
          <tr>
            <th>العملة</th><th>النوع</th><th>الاتجاه</th><th>السعر</th><th>المنفذ</th><th>الحالة</th><th>الوقت</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="7" style="text-align:center;color:var(--sub)">اضغط جلب السجل لعرض أوامر المنصة</td></tr>
        </tbody>
      </table>
    </div>
  </details>

<script>
let freeUsdt = 0;
async function loadBal(){
  try{
    const r = await fetch('/api/data');
    const d = await r.json();
    freeUsdt = d.real_balance_usdt || 0;
    document.getElementById('m-usdt').innerText = freeUsdt.toFixed(2) + ' $';
  }catch(e){}
}
loadBal(); setInterval(loadBal, 3000);

function setPct(p){ document.getElementById('t-qty').value = (freeUsdt * p).toFixed(2); }

async function execTrade(side){
  const sym = document.getElementById('t-sym').value.trim().toUpperCase();
  const type = document.getElementById('t-type').value;
  const val = parseFloat(document.getElementById('t-qty').value);
  const price = parseFloat(document.getElementById('t-price').value);

  if(!sym || !val){ alert('يرجى ملء رمز العملة والقيمة'); return; }
  if(type === 'LIMIT' && (!price || price <= 0)){ alert('يرجى تحديد السعر'); return; }

  const payload = {
    symbol: sym.endsWith('USDT') ? sym : sym + 'USDT',
    side: side,
    order_type: type,
    val: val,
    price: price
  };

  if(confirm('تأكيد تنفيذ أمر ' + side + ' لـ ' + payload.symbol + ' بقيمة ' + val + '؟')){
    const r = await fetch('/api/terminal_trade', {method:'POST', body:JSON.stringify(payload)});
    const d = await r.json();
    alert(d.msg);
    loadBal();
  }
}

async function fetchExchangeHistory(){
  const sym = document.getElementById('hist-sym').value.trim().toUpperCase();
  if(!sym){ alert("أدخل رمز العملة"); return; }
  const fullSym = sym.endsWith("USDT") ? sym : sym + "USDT";
  
  const r = await fetch('/api/exchange_orders?symbol=' + fullSym);
  const orders = await r.json();
  let h = '';
  if(Array.isArray(orders) && orders.length > 0){
    orders.forEach(o => {
      const sideColor = o.side === 'BUY' ? 'var(--success)' : 'var(--danger)';
      h += '<tr>' +
        '<td><strong>' + o.symbol + '</strong></td>' +
        '<td>' + o.type + '</td>' +
        '<td style="color:' + sideColor + ';font-weight:bold">' + o.side + '</td>' +
        '<td>' + parseFloat(o.price) + '$</td>' +
        '<td>' + parseFloat(o.executedQty) + ' / ' + parseFloat(o.origQty) + '</td>' +
        '<td><span style="font-weight:bold">' + o.status + '</span></td>' +
        '<td>' + new Date(o.time).toLocaleTimeString() + '</td>' +
      '</tr>';
    });
  } else {
    h = '<tr><td colspan="7" style="text-align:center;color:var(--sub)">لا توجد أوامر مسجلة لهذه العملة</td></tr>';
  }
  document.getElementById('mexc-history-table').querySelector('tbody').innerHTML = h;
}
</script>
</body>
</html>"""

def get_bot_html_fragment(b_key, b_name, b_tf, is_b3=False):
    pfx = b_key.lower()
    tf_markup = f"""
    <div>
      <label style="font-size:10px;color:var(--sub)">الفريم الزمني</label>
      <select id="{pfx}-tf">
        <option value="1m" {'selected' if b_tf=='1m' else ''}>1m</option>
        <option value="5m" {'selected' if b_tf=='5m' else ''}>5m</option>
        <option value="15m" {'selected' if b_tf=='15m' else ''}>15m</option>
        <option value="30m" {'selected' if b_tf=='30m' else ''}>30m</option>
        <option value="60m" {'selected' if b_tf=='60m' else ''}>1h</option>
      </select>
    </div>
    """ if not is_b3 else f"""
    <div>
      <label style="font-size:10px;color:var(--sub)">Trailing Stop</label>
      <select id="{pfx}-ts"><option value="1">مفعّل ✅</option><option value="0">معطّل ❌</option></select>
    </div>
    """

    return f"""
    <div id="t-{pfx}" class="tab-pane">
      <div class="card" style="display:flex;justify-content:space-between;align-items:center">
        <span>{b_name}: <strong id="{pfx}-st" style="color:#f59e0b">PAUSED</strong></span>
        <div style="display:flex;gap:3px">
          <button class="icon-btn" style="background:var(--success);color:#fff" title="تشغيل" onclick="setSt('{b_key}','RUNNING')">▶️</button>
          <button class="icon-btn" style="background:#f59e0b;color:#000" title="إيقاف مؤقت" onclick="setSt('{b_key}','PAUSED')">⏸️</button>
          <button class="icon-btn" style="background:var(--danger);color:#fff" title="إيقاف تام" onclick="setSt('{b_key}','STOPPED')">⏹️</button>
        </div>
      </div>
      
      <div class="stats-row">
        <div class="stat-box">
          <div class="stat-title">أرباح اليوم</div>
          <div class="stat-val" id="{pfx}-pnl">+0.00$</div>
        </div>
        <div class="stat-box">
          <div class="stat-title">نسبة الفوز</div>
          <div class="stat-val" id="{pfx}-winrate">0%</div>
        </div>
        <div class="stat-box">
          <div class="stat-title">السيولة المفتوحة</div>
          <div class="stat-val" id="{pfx}-cap-used">0$</div>
        </div>
      </div>

      <details class="card">
        <summary style="color:#a78bfa">⚙️ الإعدادات وسقف رأس المال ▾</summary>
        <div class="form-row" style="padding-top:8px">
          <div><label style="font-size:10px;color:var(--sub)">سقف رأس المال ($)</label><input type="number" id="{pfx}-alloc" value="50"></div>
          <div><label style="font-size:10px;color:var(--sub)">أقصى صفقات/عملة</label><input type="number" id="{pfx}-maxcon" value="1"></div>
          <div><label style="font-size:10px;color:var(--sub)">حجم الصفقة ($)</label><input type="number" id="{pfx}-size" value="10"></div>
          <div><label style="font-size:10px;color:var(--sub)">نوع الأمر</label>
            <select id="{pfx}-ordtype">
              <option value="CHASE_LIMIT">متتبع ليميت (Chase 0% Fee)</option>
              <option value="MARKET">سعر السوق (Market)</option>
              <option value="LIMIT">أمر معلق (Limit)</option>
            </select>
          </div>
          <div><label style="font-size:10px;color:var(--sub)">هدف الربح (TP %)</label><input type="number" id="{pfx}-tp" value="2.5" step="0.1"></div>
          <div><label style="font-size:10px;color:var(--sub)">وقف الخسارة (SL %)</label><input type="number" id="{pfx}-sl" value="1.2" step="0.1"></div>
          {tf_markup}
          <div style="display:flex;align-items:flex-end"><button class="btn" style="background:var(--primary);color:#fff;width:100%" onclick="saveBotCfg('{b_key}', '{pfx}')">💾 حفظ</button></div>
        </div>
      </details>

      <details class="card" id="{pfx}-coins-card" open>
        <summary style="display:flex;justify-content:space-between;align-items:center">
          <span>📊 جدول العملات وأرباح اليوم</span>
          <div style="display:flex;gap:3px" onclick="event.stopPropagation()">
            <button class="btn manage-ctrl" style="background:#10b981;color:#fff;font-size:10px;padding:2px 4px" onclick="addCoinToBot('{b_key}')">➕ إضافة</button>
            <button class="icon-btn" style="background:#334155;color:#fff;font-size:11px" onclick="toggleManage('{pfx}-coins-card')">⚙️</button>
          </div>
        </summary>
        <div style="overflow-x:auto;padding-top:6px">
          <table id="{pfx}-coins-table">
            <thead>
              <tr>
                <th onclick="sortTable('{pfx}-coins-table', 0)" style="cursor:pointer">العملة ⇕</th>
                <th onclick="sortTable('{pfx}-coins-table', 1)" style="cursor:pointer">السعر ⇕</th>
                <th onclick="sortTable('{pfx}-coins-table', 2)" style="cursor:pointer">ربح اليوم ⇕</th>
                <th>الصفقات</th>
                <th>دخول</th>
                <th class="manage-ctrl">حذف</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </details>

      <details class="card" id="{pfx}-orders-card" open>
        <summary style="display:flex;justify-content:space-between;align-items:center">
          <span>📂 الصفقات المفتوحة (Live PnL)</span>
          <button class="icon-btn" style="background:#334155;color:#fff;font-size:11px" onclick="toggleManage('{pfx}-orders-card'); event.stopPropagation();">⚙️</button>
        </summary>
        <div style="overflow-x:auto;padding-top:6px">
          <table id="{pfx}-orders">
            <thead>
              <tr>
                <th onclick="sortTable('{pfx}-orders', 0)" style="cursor:pointer">العملة ⇕</th>
                <th onclick="sortTable('{pfx}-orders', 1)" style="cursor:pointer">الدخول ⇕</th>
                <th>الكمية</th>
                <th onclick="sortTable('{pfx}-orders', 3)" style="cursor:pointer">Live PnL ⇕</th>
                <th>الوقت</th>
                <th class="manage-ctrl">إجراءات</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </details>

      <details class="card">
        <summary style="color:#60a5fa">📜 سجل الصفقات المغلقة والعمولات ({b_name}) ▾</summary>
        <div style="overflow-x:auto;padding-top:6px">
          <table id="{pfx}-history-table">
            <thead>
              <tr>
                <th>العملة</th><th>الدخول</th><th>الخروج</th><th>الكمية</th><th>صافي الربح</th><th>العمولة</th><th>السبب</th><th>الوقت</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </details>
    </div>
    """

def get_complete_dashboard():
    b1 = get_bot_html_fragment('BOT_1', '🤖 Bot 1 (EWO 5m)', '5m', False)
    b2a = get_bot_html_fragment('BOT_2A', '⚡ Bot 2A (Scalp 15m)', '15m', False)
    b2b = get_bot_html_fragment('BOT_2B', '⚡ Bot 2B (Swing 1h)', '60m', False)
    b2c = get_bot_html_fragment('BOT_2C', '⚡ Bot 2C (Custom TF)', '5m', False)
    b3 = get_bot_html_fragment('BOT_3', '🎯 Bot 3 (Manual Trigger)', '1m', True)

    header_and_tabs = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>MEXC Multi-Bot Hub</title>
<style>
:root{{--bg:#090d16;--card:#111827;--border:#1f293d;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--text:#f3f4f6;--sub:#94a3b8}}
*{{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif}}
body{{background:var(--bg);color:var(--text);padding:8px;line-height:1.4}}
.header-box{{display:flex;justify-content:space-between;align-items:center;padding:10px;background:var(--card);border-radius:10px;border:1px solid var(--border);margin-bottom:8px;flex-wrap:wrap;gap:6px}}
.wallet-bar{{display:flex;gap:8px;align-items:center;background:#151e30;padding:4px 8px;border-radius:6px;border:1px solid var(--border)}}
.tabs{{display:flex;gap:4px;margin:8px 0;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:2px}}
.tab{{padding:6px 10px;background:#151e30;border:1px solid var(--border);border-radius:6px;color:var(--sub);cursor:pointer;font-weight:bold;white-space:nowrap;font-size:12px}}
.tab.active{{background:var(--primary);color:#fff}}
.tab-pane{{display:none}}
.tab-pane.active{{display:block}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px;margin-bottom:8px}}
.stats-row{{display:flex;gap:6px;margin-bottom:8px}}
.stat-box{{flex:1;background:#151e30;border:1px solid var(--border);border-radius:8px;padding:6px 4px;text-align:center}}
.stat-title{{font-size:10px;color:var(--sub);margin-bottom:2px;white-space:nowrap}}
.stat-val{{font-size:13px;font-weight:bold}}
.btn{{padding:4px 8px;border:none;border-radius:5px;font-weight:bold;cursor:pointer;font-size:11px;display:inline-flex;align-items:center;justify-content:center;gap:3px}}
.icon-btn{{padding:3px 6px;font-size:13px;border-radius:5px;border:none;cursor:pointer}}
table{{width:100%;border-collapse:collapse;text-align:right}}
th,td{{padding:6px 4px;border-bottom:1px solid var(--border);font-size:11px}}
th{{color:var(--sub)}}
.badge{{padding:2px 4px;border-radius:4px;font-size:10px;font-weight:bold}}
.badge-active{{background:#10b98122;color:var(--success)}}
.badge-idle{{background:#64748b22;color:var(--sub)}}
.logs{{max-height:180px;overflow-y:auto;font-family:monospace;font-size:10.5px;background:#090d16;padding:6px;border-radius:6px;border:1px solid var(--border)}}
.log-item{{padding:2px 0;border-bottom:1px solid #1f293d44;display:flex;gap:4px}}
input,select{{background:#090d16;border:1px solid var(--border);color:#fff;padding:6px;border-radius:6px;font-size:11px;width:100%}}
details{{background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:8px;overflow:hidden}}
summary{{padding:8px 10px;cursor:pointer;font-weight:bold;background:#151e30;font-size:12px}}
.form-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:6px;padding:8px}}
.manage-ctrl{{display:none}}
.manage-mode .manage-ctrl{{display:table-cell}}
.modal-overlay{{position:fixed;top:0;left:0;right:0;bottom:0;background:#00000088;display:none;align-items:center;justify-content:center;z-index:99}}
.modal{{background:#111827;border:1px solid var(--border);padding:14px;border-radius:10px;width:290px}}
</style>
</head>
<body>
  <div class="header-box">
    <div>
      <div style="display:flex;align-items:center;gap:6px">
        <strong>🎛️ Command Hub</strong>
        <a href="/analytics" style="color:#60a5fa;font-size:11px;text-decoration:none;background:#1e293b;padding:2px 6px;border-radius:4px">📊 تداول يدوي ↗</a>
      </div>
      <div style="font-size:10px;color:var(--sub)">Live PnL الكلي: <strong id="total-live-pnl-val" style="color:#10b981">+0.00$</strong></div>
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      <div class="wallet-bar">
        <div><span style="font-size:10px;color:var(--sub)">USDT:</span> <strong id="live-usdt" style="color:#10b981;font-size:12px">0.00 $</strong></div>
        <div style="border-right:1px solid var(--border);padding-right:6px"><span style="font-size:10px;color:var(--sub)">المحفظة:</span> <strong id="live-total-usd" style="color:#38bdf8;font-size:12px">0.00 $</strong></div>
      </div>
      <span id="api-stat" style="font-size:11px">فحص...</span>
      <button class="icon-btn" style="background:#334155;color:#fff" title="خروج" onclick="fetch('/api/logout').then(function(){{location.href='/login'}})">🚪</button>
    </div>
  </div>

  <details id="keys-box">
    <summary style="color:#60a5fa">🔑 إعدادات المفاتيح و IP السيرفر ▾ <span id="keys-status-badge"></span></summary>
    <div style="padding:8px">
      <div style="background:#090d16;padding:5px 8px;border-radius:6px;border:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-size:10px;color:var(--sub)">IP: <strong id="server-ip-val" style="color:#38bdf8;font-family:monospace">جاري...</strong></span>
        <button class="btn" style="background:#0284c7;color:#fff;font-size:10px;padding:2px 6px" onclick="copyServerIP()">📋 نسخ</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:4px">
        <input type="password" id="m-key" placeholder="API Key">
        <input type="password" id="m-sec" placeholder="API Secret">
      </div>
      <button class="btn" style="background:var(--primary);color:#fff;width:100%;margin-bottom:6px" onclick="saveKeys()">💾 حفظ المفاتيح</button>
      <div style="display:grid;grid-template-columns:1fr auto;gap:4px">
        <input type="password" id="new-pass" placeholder="كلمة المرور الجديدة">
        <button class="btn" style="background:#10b981;color:#fff" onclick="changePass()">تحديث 🔒</button>
      </div>
    </div>
  </details>

  <div class="tabs">
    <button class="tab active" onclick="showTab('t-bot_1', this)">🤖 Bot 1</button>
    <button class="tab" onclick="showTab('t-bot_2a', this)">⚡ Bot 2A</button>
    <button class="tab" onclick="showTab('t-bot_2b', this)">⚡ Bot 2B</button>
    <button class="tab" onclick="showTab('t-bot_2c', this)">⚡ Bot 2C</button>
    <button class="tab" onclick="showTab('t-bot_3', this)">🎯 Bot 3</button>
    <button class="tab" onclick="showTab('t-w', this)">💰 المحفظة</button>
  </div>

  {b1}
  {b2a}
  {b2b}
  {b2c}
  {b3}

  <!-- Wallet Tab -->
  <div id="t-w" class="tab-pane">
    <details class="card" id="wallet-card" open>
      <summary style="display:flex;justify-content:space-between;align-items:center">
        <span>💼 المحفظة (الرصيد الحر والمربوط بالبوتات)</span>
        <div style="display:flex;gap:4px" onclick="event.stopPropagation()">
          <button class="btn" style="background:#0284c7;color:#fff;font-size:10px" onclick="triggerUpdate()">🔄 تحديث</button>
          <button class="btn manage-ctrl" style="background:#8b5cf6;color:#fff;font-size:10px" onclick="convertDustDirect()">🔄 تحويل لـ MX</button>
          <button class="btn manage-ctrl" style="background:var(--danger);color:#fff;font-size:10px" onclick="panicSellAll()">🔥 تسييل الكل</button>
          <button class="icon-btn" style="background:#334155;color:#fff;font-size:11px" onclick="toggleManage('wallet-card')">⚙️</button>
        </div>
      </summary>
      <div style="overflow-x:auto;padding-top:6px">
        <table id="w-table">
          <thead>
            <tr>
              <th onclick="sortTable('w-table', 0)" style="cursor:pointer">العملة ⇕</th>
              <th>المتاح الكلي</th>
              <th>مربوط ببوتات</th>
              <th>حر غير مربوط</th>
              <th>القيمة ($)</th>
              <th class="manage-ctrl">تسييل مخصص</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </details>

    <details class="card" id="pending-card" open>
      <summary style="display:flex;justify-content:space-between;align-items:center">
        <span>⏳ الأوامر المعلقة في MEXC</span>
        <button class="icon-btn manage-ctrl" style="background:#334155;color:#fff;font-size:11px" onclick="toggleManage('pending-card')">⚙️</button>
      </summary>
      <div style="overflow-x:auto;padding-top:6px">
        <table id="limit-orders-table">
          <thead><tr><th>العملة</th><th>النوع</th><th>السعر</th><th>الكمية</th><th>الوقت</th><th class="manage-ctrl">إلغاء</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </details>
  </div>

  <div class="modal-overlay" id="edit-modal">
    <div class="modal">
      <h4 style="margin-bottom:6px;font-size:12px">✏️ تعديل الصفقة</h4>
      <input type="hidden" id="edit-pos-id">
      <div style="margin-bottom:4px"><label style="font-size:10px;color:var(--sub)">سعر الدخول ($):</label><input type="number" id="edit-entry" step="any"></div>
      <div style="margin-bottom:4px"><label style="font-size:10px;color:var(--sub)">الكمية:</label><input type="number" id="edit-qty" step="any"></div>
      <div style="margin-bottom:4px"><label style="font-size:10px;color:var(--sub)">جني الأرباح (TP %):</label><input type="number" id="edit-tp" step="0.1"></div>
      <div style="margin-bottom:6px"><label style="font-size:10px;color:var(--sub)">وقف الخسارة (SL %):</label><input type="number" id="edit-sl" step="0.1"></div>
      <div style="display:flex;gap:4px">
        <button class="btn" style="background:#10b981;color:#fff;width:100%" onclick="saveEditedPos()">💾 حفظ</button>
        <button class="btn" style="background:#334155;color:#fff;width:100%" onclick="closeEditModal()">إلغاء</button>
      </div>
    </div>
  </div>

  <div class="modal-overlay" id="panic-modal">
    <div class="modal">
      <h4 style="margin-bottom:6px;font-size:12px">🔥 تسييل العملة</h4>
      <input type="hidden" id="panic-asset">
      <div style="font-size:11px;margin-bottom:8px" id="panic-info-text"></div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <button class="btn" style="background:#f59e0b;color:#000" onclick="execCustomPanic('UNLINKED')">بيع الفائض الحر فقط</button>
        <button class="btn" style="background:var(--danger);color:#fff" onclick="execCustomPanic('ALL')">بيع الرصيد كاملاً وتصفير البوتات</button>
        <button class="btn" style="background:#334155;color:#fff" onclick="document.getElementById('panic-modal').style.display='none'">إلغاء</button>
      </div>
    </div>
  </div>

  <details class="card" open>
    <summary style="display:flex;justify-content:space-between;align-items:center">
      <span>📜 السجل المباشر</span>
      <div style="display:flex;gap:4px" onclick="event.stopPropagation()">
        <input type="text" id="log-search" placeholder="🔍 بحث..." style="width:80px;padding:2px 4px;font-size:10px" oninput="renderLogs()">
        <button class="icon-btn" style="background:#334155;color:#fff" title="نسخ" onclick="copyLogs()">📋</button>
      </div>
    </summary>
    <div style="padding-top:6px">
      <div style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:4px">
        <button class="btn" style="background:#151e30;color:var(--sub);font-size:9.5px" onclick="setLogFilter('all', this)">الكل</button>
        <button class="btn" style="background:#151e30;color:var(--sub);font-size:9.5px" onclick="setLogFilter('buys', this)">🚀 شراء</button>
        <button class="btn" style="background:#151e30;color:var(--sub);font-size:9.5px" onclick="setLogFilter('sells', this)">💰 بيع</button>
        <button class="btn" style="background:#151e30;color:var(--sub);font-size:9.5px" onclick="setLogFilter('orders', this)">📤 أوامر</button>
      </div>
      <div class="logs" id="logs"></div>
    </div>
  </details>

<script>
var BOT_KEYS_JS = ['BOT_1', 'BOT_2A', 'BOT_2B', 'BOT_2C', 'BOT_3'];
var rawLogs = [];
var logsText = "";
var currentLogFilter = "all";
var startTs = Date.now();
var keysLoaded = false;
var currentPublicIP = "";
var initialConfigsPopulated = false;

document.addEventListener('DOMContentLoaded', function(){{
  var firstPane = document.getElementById('t-bot_1');
  if(firstPane) firstPane.classList.add('active');
}});

function showTab(id, btn){{
  document.querySelectorAll('.tab-pane').forEach(function(p){{ p.classList.remove('active'); }});
  document.querySelectorAll('.tab').forEach(function(b){{ b.classList.remove('active'); }});
  var target = document.getElementById(id);
  if(target) target.classList.add('active');
  if(btn) btn.classList.add('active');
}}

function setLogFilter(cat, btn){{
  currentLogFilter = cat;
  renderLogs();
}}

function copyLogs(){{
  navigator.clipboard.writeText(logsText).then(function(){{ alert("✅ تم نسخ السجلات!"); }});
}}

function copyServerIP(){{
  if(currentPublicIP && currentPublicIP !== "جاري الجلب..."){{
    navigator.clipboard.writeText(currentPublicIP).then(function(){{ alert("IP: " + currentPublicIP); }});
  }}
}}

function toggleManage(cardId){{
  var el = document.getElementById(cardId);
  if(el) el.classList.toggle('manage-mode');
}}

function triggerUpdate(){{
  update();
  alert("🔄 جاري التحديث من المنصة...");
}}

function sortTable(tableId, colIdx){{
  var table = document.getElementById(tableId);
  if(!table) return;
  var tbody = table.querySelector('tbody');
  var rows = Array.from(tbody.querySelectorAll('tr'));
  if(rows.length <= 1) return;

  var isAsc = table.getAttribute('data-sort-dir') !== 'asc';
  table.setAttribute('data-sort-dir', isAsc ? 'asc' : 'desc');

  rows.sort(function(a, b){{
    var aText = a.children[colIdx] ? a.children[colIdx].innerText.replace(/[$%+]/g, '').trim() : '';
    var bText = b.children[colIdx] ? b.children[colIdx].innerText.replace(/[$%+]/g, '').trim() : '';
    var aNum = parseFloat(aText);
    var bNum = parseFloat(bText);

    if(!isNaN(aNum) && !isNaN(bNum)){{
      return isAsc ? aNum - bNum : bNum - aNum;
    }}
    return isAsc ? aText.localeCompare(bText) : bText.localeCompare(aText);
  }});

  rows.forEach(function(r){{ tbody.appendChild(r); }});
}}

async function setSt(b, s){{
  await fetch('/api/control', {{method:'POST', body:JSON.stringify({{bot_name:b, status:s}})}});
  update();
}}

async function saveKeys(){{
  var k = document.getElementById('m-key').value.replace(/\\s+/g, '');
  var s = document.getElementById('m-sec').value.replace(/\\s+/g, '');
  if(!k || !s){{ alert("يرجى إدخال المفتاح والسر"); return; }}
  await fetch('/api/save_keys', {{method:'POST', body:JSON.stringify({{api_key:k, api_secret:s}})}});
  alert('✅ تم حفظ مفاتيح MEXC!');
  update();
}}

async function changePass(){{
  var p = document.getElementById('new-pass').value;
  if(!p){{ alert("أدخل كلمة المرور"); return; }}
  var res = await fetch('/api/change_password', {{method:'POST', body:JSON.stringify({{new_password:p}})}});
  if(res.ok){{ alert("✅ تم التغيير!"); document.getElementById('new-pass').value = ''; }}
}}

async function addCoinToBot(botName){{
  var coin = prompt('أدخل رمز العملة لـ ' + botName + ' (مثال: SOL أو BTC):');
  if(coin && coin.trim()){{
    var r = await fetch('/api/add_symbol', {{method: 'POST', body: JSON.stringify({{bot_name: botName, symbol: coin.trim().toUpperCase()}})}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

async function removeCoinFromBot(botName, sym){{
  if(confirm('حذف (' + sym + ') من ' + botName + '؟')){{
    var r = await fetch('/api/remove_symbol', {{method: 'POST', body: JSON.stringify({{bot_name: botName, symbol: sym}})}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

async function saveBotCfg(botName, prefix){{
  var payload = {{
    bot_name: botName,
    max_allocation_usdt: parseFloat(document.getElementById(prefix+'-alloc').value)||50,
    max_concurrent_per_coin: parseInt(document.getElementById(prefix+'-maxcon').value)||1,
    trade_size_usdt: parseFloat(document.getElementById(prefix+'-size').value)||10,
    order_exec_type: document.getElementById(prefix+'-ordtype') ? document.getElementById(prefix+'-ordtype').value : 'CHASE_LIMIT',
    tp_pct: (parseFloat(document.getElementById(prefix+'-tp').value)||2.5) / 100.0,
    sl_pct: (parseFloat(document.getElementById(prefix+'-sl').value)||1.2) / 100.0
  }};
  var tfEl = document.getElementById(prefix+'-tf');
  if(tfEl) payload.timeframe = tfEl.value;
  var tsEl = document.getElementById(prefix+'-ts');
  if(tsEl) payload.trailing_stop = parseInt(tsEl.value);

  await fetch('/api/save_bot_config', {{method:'POST', body:JSON.stringify(payload)}});
  alert('✅ تم حفظ إعدادات ' + botName + '!');
  update();
}}

async function triggerBuy(botName, sym){{
  if(confirm('إطلاق شراء لـ ' + sym + ' عبر ' + botName + '؟')){{
    var r = await fetch('/api/manual_buy', {{method:'POST', body:JSON.stringify({{bot_name:botName, symbol:sym}})}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

async function closeSinglePos(botName, sym, posId){{
  if(confirm('تسييل ' + sym + ' فوري بسعر السوق؟')){{
    var r = await fetch('/api/close_position', {{method:'POST', body:JSON.stringify({{bot_name:botName, symbol:sym, pos_id:posId}})}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

async function unlinkPos(botName, sym, posId){{
  if(confirm('إلغاء وإزالة الصفقة من البوت دون بيعها في المنصة؟')){{
    var r = await fetch('/api/unlink_position', {{method:'POST', body:JSON.stringify({{bot_name:botName, symbol:sym, pos_id:posId}})}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

function openEditModal(posId, entry, qty, tp, sl){{
  document.getElementById('edit-pos-id').value = posId;
  document.getElementById('edit-entry').value = entry;
  document.getElementById('edit-qty').value = qty;
  document.getElementById('edit-tp').value = ((tp||0.025)*100).toFixed(2);
  document.getElementById('edit-sl').value = ((sl||0.012)*100).toFixed(2);
  document.getElementById('edit-modal').style.display = 'flex';
}}
function closeEditModal(){{
  document.getElementById('edit-modal').style.display = 'none';
}}
async function saveEditedPos(){{
  var posId = document.getElementById('edit-pos-id').value;
  var payload = {{
    pos_id: posId,
    entry_price: parseFloat(document.getElementById('edit-entry').value),
    qty: parseFloat(document.getElementById('edit-qty').value),
    tp_pct: (parseFloat(document.getElementById('edit-tp').value)||2.5)/100.0,
    sl_pct: (parseFloat(document.getElementById('edit-sl').value)||1.2)/100.0
  }};
  await fetch('/api/edit_position', {{method:'POST', body:JSON.stringify(payload)}});
  closeEditModal();
  alert("✅ تم التعديل وحفظ البيانات!");
  update();
}}

function openPanicModal(asset, free, alloc, unlinked){{
  document.getElementById('panic-asset').value = asset;
  document.getElementById('panic-info-text').innerHTML =
    'عملة: <strong>' + asset + '</strong><br>' +
    'المتاح الكلي: <strong>' + free + '</strong><br>' +
    'مربوط بالبوتات: <strong>' + alloc + '</strong><br>' +
    'الحر الفائض: <strong>' + unlinked + '</strong>';
  document.getElementById('panic-modal').style.display = 'flex';
}}

async function execCustomPanic(mode){{
  var asset = document.getElementById('panic-asset').value;
  var r = await fetch('/api/panic_custom', {{method:'POST', body:JSON.stringify({{asset:asset, mode:mode}})}});
  var d = await r.json();
  document.getElementById('panic-modal').style.display = 'none';
  alert(d.msg);
  update();
}}

async function cancelLimitOrder(sym, orderId){{
  if(confirm('إلغاء الأمر ' + orderId + ' لـ ' + sym + ' في MEXC؟')){{
    var r = await fetch('/api/cancel_order', {{method:'POST', body:JSON.stringify({{symbol:sym, order_id:orderId}})}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

async function convertDustDirect(){{
  if(confirm("تحويل الأرصدة الصغيرة لـ MX؟")){{
    var r = await fetch('/api/convert_dust_direct', {{method:'POST'}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

async function panicSellAll(){{
  if(confirm("⚠️ تسييل كل العملات لـ USDT؟")){{
    var r = await fetch('/api/panic_all', {{method:'POST'}});
    var d = await r.json();
    alert(d.msg);
    update();
  }}
}}

function renderLogs(){{
  var searchQ = (document.getElementById('log-search').value || '').toLowerCase();
  var lHtml = '';
  logsText = '';

  rawLogs.forEach(function(l){{
    var cat = l.cat || 'system';
    var msgLower = (l.msg || '').toLowerCase();
    if(currentLogFilter !== 'all' && cat !== currentLogFilter) return;
    if(searchQ && !msgLower.includes(searchQ) && !cat.includes(searchQ)) return;

    var typeColor = '#f3f4f6';
    if(l.type === 'danger') typeColor = 'var(--danger)';
    else if(l.type === 'success') typeColor = 'var(--success)';
    else if(l.type === 'warning') typeColor = '#f59e0b';
    else if(l.type === 'primary') typeColor = '#60a5fa';

    lHtml += '<div class="log-item"><span style="color:var(--sub)">[' + l.time + ']</span> <span style="color:' + typeColor + '">' + l.msg + '</span></div>';
    logsText += '[' + l.time + '] ' + l.msg + '\\n';
  }});
  document.getElementById('logs').innerHTML = lHtml || '<div style="color:var(--sub);text-align:center">لا توجد أحداث</div>';
}}

async function update(){{
  try{{
    var res = await fetch('/api/data');
    if(res.status===401){{ location.href='/login'; return; }}
    var d = await res.json();
    
    if(d.server_public_ip){{
      currentPublicIP = d.server_public_ip;
      document.getElementById('server-ip-val').innerText = d.server_public_ip;
    }}

    document.getElementById('live-usdt').innerText = (d.real_balance_usdt || 0.0).toFixed(2) + ' $';
    document.getElementById('live-total-usd').innerText = (d.total_wallet_usd_value || 0.0).toFixed(2) + ' $';
    
    var livePnlVal = d.total_live_pnl || 0.0;
    var pnlEl = document.getElementById('total-live-pnl-val');
    pnlEl.innerText = (livePnlVal >= 0 ? '+' : '') + livePnlVal.toFixed(2) + ' $';
    pnlEl.style.color = livePnlVal >= 0 ? 'var(--success)' : 'var(--danger)';

    document.getElementById('api-stat').innerHTML = d.api_connected ? '<span style="color:var(--success)">🟢 متصل</span>' : '<span style="color:var(--danger)">🔴 مفصول</span>';

    if(d.has_saved_keys){{
      document.getElementById('keys-status-badge').innerHTML = '<span style="color:var(--success);font-weight:bold">(' + d.masked_key + ')</span>';
      if(!keysLoaded){{
        document.getElementById('m-key').placeholder = 'محفوظ (' + d.masked_key + ')';
        document.getElementById('m-sec').placeholder = "محفوظ (*********)";
        keysLoaded = true;
      }}
    }}

    if(!initialConfigsPopulated && d.bots){{
      BOT_KEYS_JS.forEach(function(bKey){{
        var pfx = bKey.toLowerCase();
        var bObj = d.bots[bKey];
        if(bObj){{
          if(document.getElementById(pfx+'-alloc') && bObj.max_allocation) document.getElementById(pfx+'-alloc').value = bObj.max_allocation;
          if(document.getElementById(pfx+'-size') && bObj.trade_size) document.getElementById(pfx+'-size').value = bObj.trade_size;
          if(document.getElementById(pfx+'-maxcon') && bObj.max_concurrent) document.getElementById(pfx+'-maxcon').value = bObj.max_concurrent;
          if(document.getElementById(pfx+'-ordtype') && bObj.order_exec_type) document.getElementById(pfx+'-ordtype').value = bObj.order_exec_type;
          if(document.getElementById(pfx+'-tp') && bObj.tp_pct !== undefined) document.getElementById(pfx+'-tp').value = bObj.tp_pct;
          if(document.getElementById(pfx+'-sl') && bObj.sl_pct !== undefined) document.getElementById(pfx+'-sl').value = bObj.sl_pct;
          if(document.getElementById(pfx+'-tf') && bObj.timeframe) document.getElementById(pfx+'-tf').value = bObj.timeframe;
          if(document.getElementById(pfx+'-ts') && bObj.trailing_stop !== undefined) document.getElementById(pfx+'-ts').value = bObj.trailing_stop;
        }}
      }});
      initialConfigsPopulated = true;
    }}

    BOT_KEYS_JS.forEach(function(bKey){{
      try{{
        var pfx = bKey.toLowerCase();
        var bObj = d.bots ? d.bots[bKey] : null;
        if(!bObj) return;

        var stEl = document.getElementById(pfx+'-st');
        if(stEl){{
          stEl.innerText = bObj.status || 'PAUSED';
          stEl.style.color = bObj.status === 'RUNNING' ? '#10b981' : (bObj.status === 'PAUSED' ? '#f59e0b' : '#ef4444');
        }}

        var pnl = bObj.daily_pnl || 0.0;
        var pnlEl = document.getElementById(pfx+'-pnl');
        if(pnlEl){{
          pnlEl.innerText = (pnl >= 0 ? '+' : '') + pnl.toFixed(3) + '$';
          pnlEl.style.color = pnl >= 0 ? 'var(--success)' : 'var(--danger)';
        }}

        var totalT = bObj.trades_count || 0;
        var winT = bObj.winning_count || 0;
        var wr = totalT > 0 ? ((winT / totalT) * 100).toFixed(0) : '0';
        var wrEl = document.getElementById(pfx+'-winrate');
        if(wrEl) wrEl.innerText = wr + '% (' + totalT + ')';

        var totalOpen = 0;
        var coinsTableHtml = '';
        (bObj.symbols || []).forEach(function(sym){{
          var count = (bObj.active_positions && bObj.active_positions[sym]) ? bObj.active_positions[sym].length : 0;
          totalOpen += count;
          var coinPnl = (bObj.daily_pnl_coins && bObj.daily_pnl_coins[sym]) ? bObj.daily_pnl_coins[sym] : 0.0;
          var price = (d.market_prices && d.market_prices[sym]) ? d.market_prices[sym].bid : 0.0;

          coinsTableHtml += '<tr>' +
            '<td><strong>' + sym + '</strong></td>' +
            '<td>' + (price ? price.toFixed(4)+'$' : '-') + '</td>' +
            '<td style="color:' + (coinPnl>=0?'var(--success)':'var(--danger)') + ';font-weight:bold">' + (coinPnl>=0?'+':'') + coinPnl.toFixed(3) + '$</td>' +
            '<td><span class="badge ' + (count>0?'badge-active':'badge-idle') + '">' + count + '/' + (bObj.max_concurrent||1) + '</span></td>' +
            '<td><button class="icon-btn" style="background:var(--primary);color:#fff" title="شراء" onclick="triggerBuy(\\'' + bKey + '\\',\\'' + sym + '\\')">⚡</button></td>' +
            '<td class="manage-ctrl"><button class="icon-btn" style="background:#334155;color:#f87171" title="حذف" onclick="removeCoinFromBot(\\'' + bKey + '\\',\\'' + sym + '\\')">🗑️</button></td>' +
          '</tr>';
        }});

        var coinsTable = document.getElementById(pfx+'-coins-table');
        if(coinsTable){{
          coinsTable.querySelector('tbody').innerHTML = coinsTableHtml || '<tr><td colspan="6" style="text-align:center">لا توجد عملات</td></tr>';
        }}
        
        var usedUsd = totalOpen * (bObj.trade_size || 10);
        var capEl = document.getElementById(pfx+'-cap-used');
        if(capEl) capEl.innerText = usedUsd.toFixed(0) + '/' + (bObj.max_allocation||50) + '$';

        var posHtml = '';
        if(bObj.active_positions){{
          for(var s in bObj.active_positions){{
            var curBid = (d.market_prices && d.market_prices[s]) ? d.market_prices[s].bid : 0.0;
            (bObj.active_positions[s] || []).forEach(function(p){{
              var livePnlVal = curBid ? (curBid - p.entry_price) * p.qty : 0.0;
              var livePnlPct = (curBid && p.entry_price > 0) ? ((curBid - p.entry_price) / p.entry_price) * 100.0 : 0.0;
              var pnlColor = livePnlVal >= 0 ? 'var(--success)' : 'var(--danger)';

              posHtml += '<tr>' +
                '<td><strong>' + s + '</strong></td>' +
                '<td>' + p.entry_price + '$</td>' +
                '<td>' + p.qty + '</td>' +
                '<td style="color:' + pnlColor + ';font-weight:bold">' + (livePnlVal>=0?'+':'') + livePnlVal.toFixed(3) + '$ (' + livePnlPct.toFixed(2) + '%)</td>' +
                '<td>' + p.time + '</td>' +
                '<td class="manage-ctrl">' +
                  '<div style="display:flex;gap:3px">' +
                    '<button class="icon-btn" style="background:var(--danger);color:#fff" title="تسييل" onclick="closeSinglePos(\\'' + bKey + '\\',\\'' + s + '\\', \\'' + p.id + '\\')">🔥</button>' +
                    '<button class="icon-btn" style="background:#3b82f6;color:#fff" title="تعديل" onclick="openEditModal(\\'' + p.id + '\\', ' + p.entry_price + ', ' + p.qty + ', ' + (p.tp_pct||0.025) + ', ' + (p.sl_pct||0.012) + ')">✏️</button>' +
                    '<button class="icon-btn" style="background:#475569;color:#fca5a5" title="فك ربط" onclick="unlinkPos(\\'' + bKey + '\\',\\'' + s + '\\', \\'' + p.id + '\\')">🚫</button>' +
                  '</div>' +
                '</td>' +
              '</tr>';
            }});
          }}
        }}
        var ordersTable = document.getElementById(pfx+'-orders');
        if(ordersTable){{
          ordersTable.querySelector('tbody').innerHTML = posHtml || '<tr><td colspan="6" style="text-align:center;color:var(--sub)">لا توجد صفقات</td></tr>';
        }}

        fetch('/api/history?bot_name=' + bKey).then(function(r){{ return r.json(); }}).then(function(hList){{
          var hHtml = '';
          hList.forEach(function(h){{
            var netColor = h.net_pnl >= 0 ? 'var(--success)' : 'var(--danger)';
            hHtml += '<tr>' +
              '<td><strong>' + h.symbol + '</strong></td>' +
              '<td>' + h.entry_price + '$</td>' +
              '<td>' + h.exit_price + '$</td>' +
              '<td>' + h.qty + '</td>' +
              '<td style="color:' + netColor + ';font-weight:bold">' + (h.net_pnl>=0?'+':'') + h.net_pnl.toFixed(3) + '$</td>' +
              '<td style="color:var(--sub)">' + h.fee_usd.toFixed(3) + '$</td>' +
              '<td><span class="badge">' + h.reason + '</span></td>' +
              '<td>' + h.exit_time + '</td>' +
            '</tr>';
          }});
          var hTable = document.getElementById(pfx+'-history-table');
          if(hTable) hTable.querySelector('tbody').innerHTML = hHtml || '<tr><td colspan="8" style="text-align:center;color:var(--sub)">لا توجد صفقات مغلقة مؤرشفة</td></tr>';
        }}).catch(function(e){{}});

      }}catch(err){{}}
    }});

    var wHtml = '';
    (d.wallet_assets||[]).forEach(function(a){{
      var canSell = a.asset !== 'USDT' && a.free > 0;
      var priceStr = a.asset === 'USDT' ? '1.00 $' : (a.usd_price > 0 ? a.usd_price.toFixed(4) + ' $' : '-');
      var valStr = a.usd_value > 0 ? a.usd_value.toFixed(2) + ' $' : '0.00 $';
      
      wHtml += '<tr>' +
        '<td><strong>' + a.asset + '</strong></td>' +
        '<td>' + a.free + '</td>' +
        '<td style="color:#60a5fa">' + (a.bot_alloc || 0) + '</td>' +
        '<td style="color:#10b981;font-weight:bold">' + (a.unlinked_free !== undefined ? a.unlinked_free : a.free) + '</td>' +
        '<td style="color:#38bdf8;font-weight:bold">' + valStr + '</td>' +
        '<td class="manage-ctrl">' +
          (canSell ? '<button class="btn" style="background:#f59e0b;color:#000;font-size:10px" onclick="openPanicModal(\\'' + a.asset + '\\', ' + a.free + ', ' + (a.bot_alloc||0) + ', ' + (a.unlinked_free||a.free) + ')">🔥 تسييل</button>' : '-') +
        '</td>' +
      '</tr>';
    }});
    var wTable = document.getElementById('w-table');
    if(wTable){{
      wTable.querySelector('tbody').innerHTML = wHtml || '<tr><td colspan="6" style="text-align:center">لا توجد أرصدة</td></tr>';
    }}

    var ordHtml = '';
    (d.open_limit_orders || []).forEach(function(o){{
      ordHtml += '<tr>' +
        '<td><strong>' + o.symbol + '</strong></td>' +
        '<td style="color:' + (o.side==='BUY'?'var(--success)':'var(--danger)') + ';font-weight:bold">' + o.side + '</td>' +
        '<td>' + parseFloat(o.price) + '$</td>' +
        '<td>' + parseFloat(o.origQty) + '</td>' +
        '<td>' + new Date(o.time).toLocaleTimeString() + '</td>' +
        '<td class="manage-ctrl"><button class="icon-btn" style="background:#dc2626;color:#fff" title="إلغاء" onclick="cancelLimitOrder(\\'' + o.symbol + '\\', \\'' + o.orderId + '\\')">❌</button></td>' +
      '</tr>';
    }});
    var limitTable = document.getElementById('limit-orders-table');
    if(limitTable){{
      limitTable.querySelector('tbody').innerHTML = ordHtml || '<tr><td colspan="6" style="text-align:center;color:var(--sub)">لا توجد أوامر معلقة</td></tr>';
    }}

    rawLogs = d.recent_logs || [];
    renderLogs();
  }}catch(e){{}}
}}
setInterval(update, 2000);
update();
</script>
</body>
</html>"""
    return header_and_tabs

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
            trades = database.get_closed_trades(bot_name)
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(trades, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/analytics':
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            self.wfile.write(ANALYTICS_HTML.encode('utf-8'))
        elif self.path == '/api/logout':
            self.send_response(200); self.send_header('Set-Cookie', 'session_id=; Path=/; Max-Age=0'); self.end_headers()
        else:
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            self.wfile.write(get_complete_dashboard().encode('utf-8'))

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
            st = data.get("status", "PAUSED")
            database.update_bot_config(b_name, {"status": st})
            if b_name in shared_state["bots"]:
                shared_state["bots"][b_name]["status"] = st
            add_log(f"تغيير حالة {b_name} إلى: {st}", "system", "info")
            self.send_response(200); self.end_headers()

        elif self.path == '/api/save_bot_config':
            b_name = data.pop("bot_name", "BOT_1")
            database.update_bot_config(b_name, data)
            add_log(f"تم حفظ إعدادات {b_name}", "system", "info")
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
            bid, ask = get_orderbook(sym)
            if ask:
                q = float(format_quantity(sym, size / ask))
                ok, res = place_order(sym, "BUY", qty=q, quote_qty=size)
                if ok:
                    trade_id = f"{b_name.lower()}_{int(time.time()*1000)}"
                    time_str = datetime.now(timezone.utc).strftime("%H:%M")
                    t_obj = {
                        'id': trade_id, 'bot_name': b_name, 'symbol': sym,
                        'entry_price': ask, 'highest_price': ask, 'qty': q,
                        'tp_pct': float(cfg.get("tp_pct", 0.025)),
                        'sl_pct': float(cfg.get("sl_pct", 0.012)),
                        'time_str': time_str
                    }
                    database.insert_active_trade(t_obj)
                    if sym not in shared_state["bots"][b_name]["active_positions"]:
                        shared_state["bots"][b_name]["active_positions"][sym] = []
                    shared_state["bots"][b_name]["active_positions"][sym].append({
                        'id': trade_id, 'entry_price': ask, 'highest_price': ask, 'qty': q,
                        'tp_pct': t_obj['tp_pct'], 'sl_pct': t_obj['sl_pct'], 'time': time_str
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
            base_asset = sym.replace("USDT", "").replace("USDC", "")

            new_positions = []
            found = False
            for p in shared_state["bots"][b_name]["active_positions"].get(sym, []):
                if p.get("id") == pos_id and not found:
                    found = True
                    avail = get_asset_free_balance(base_asset)
                    sell_qty = min(p['qty'], avail)

                    if float(format_quantity(sym, sell_qty)) > 0:
                        ok, res = place_order(sym, "SELL", qty=sell_qty)
                        cur_price = bid if bid else p['entry_price']
                        gross_pnl = (cur_price - p['entry_price']) * sell_qty
                        fee_usd = (p['entry_price'] * sell_qty * 0.001) + (cur_price * sell_qty * 0.001)
                        net_pnl = gross_pnl - fee_usd

                        shared_state["bots"][b_name]["daily_pnl"] += net_pnl
                        shared_state["bots"][b_name]["daily_pnl_coins"][sym] += net_pnl
                        shared_state["bots"][b_name]["trades_count"] += 1
                        if net_pnl > 0: shared_state["bots"][b_name]["winning_count"] += 1
                        
                        database.archive_closed_trade({
                            "id": pos_id, "bot_name": b_name, "symbol": sym,
                            "entry_price": p["entry_price"], "exit_price": cur_price,
                            "qty": sell_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                            "net_pnl": net_pnl, "reason": "يدوي (Manual)",
                            "entry_time": p["time"], "exit_time": datetime.now(timezone.utc).strftime("%H:%M")
                        })
                        add_log(f"🔥 تسييل {sym} في {b_name} بسعر {cur_price}$ | صافي: {net_pnl:+.3f}$", "sells", "danger")
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
                        msg = f"✅ تم تسييل كامل الرصيد ({free_qty} {asset})"
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
            for a in shared_state.get("wallet_assets", []):
                asset = a["asset"]
                free_qty = float(a["free"])
                val_usd = float(a.get("usd_value", 0.0))
                if asset not in ["USDT", "USDC", "MX"] and val_usd < 5.0 and free_qty > 0:
                    sym = f"{asset}USDT"
                    ok, res = place_order(sym, "SELL", qty=free_qty, order_type="MARKET")
                    if ok: total_sold_usd += val_usd
            if total_sold_usd > 1.0:
                place_order("MXUSDT", "BUY", quote_qty=total_sold_usd, order_type="MARKET")
                msg = f"✅ تم تحويل الأرصدة الصغيرة لـ MX بقيمة {total_sold_usd:.2f}$"
            else:
                msg = "لا توجد أرصدة صغيرة للتحويل"
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
                        for bKey in BOT_KEYS:
                            for p in shared_state["bots"][bKey]["active_positions"].get(sym, []):
                                database.delete_active_trade(p["id"])
                            shared_state["bots"][bKey]["active_positions"][sym] = []
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
