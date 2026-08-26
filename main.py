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
    "sniper_positions": [],
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

            # 🎯 متابعة صفقات القناص المستقلة (Break-even & Trailing Stop)
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

                if bid > highest:
                    highest = bid
                    sp["highest_price"] = highest
                    database.update_sniper_trade(sp["id"], {"highest_price": highest})

                tp_price = entry * (1.0 + sp.get("tp_pct", 0.03))
                sl_price = entry * (1.0 - sp.get("sl_pct", 0.015))

                # ميزة Break-Even: إذا حقق السعر نصف هدف الـ TP، يتم تأمين الصفقة على سعر الدخول
                if not sp.get("is_break_even") and (highest >= entry * (1.0 + (sp.get("tp_pct", 0.03) * 0.5))):
                    sp["is_break_even"] = 1
                    database.update_sniper_trade(sp["id"], {"is_break_even": 1})
                    add_log(f"🛡️ [Sniper] تأمين صفقة {sym} بنقل الوقف لسعر الدخول (Break-Even)", "system", "success")

                effective_sl = entry if sp.get("is_break_even") else sl_price

                # ميزة Trailing Stop للقناص
                cb_pct = sp.get("trailing_cb", 0.008)
                trailing_sl = highest * (1.0 - cb_pct)
                if highest >= entry * (1.0 + cb_pct):
                    effective_sl = max(effective_sl, trailing_sl)

                hit_tp = bid >= tp_price
                hit_sl = bid <= effective_sl

                if hit_tp or hit_sl:
                    reason = "🎯 TP القناص" if hit_tp else ("🔄 TS القناص" if effective_sl > sl_price else "🛑 SL القناص")
                    avail = get_asset_free_balance(base_asset)
                    sell_qty = min(sp["qty"], avail)

                    if float(format_quantity(sym, sell_qty)) > 0:
                        ok, res = place_order(sym, "SELL", qty=sell_qty, order_type="MARKET")
                        if ok:
                            gross_pnl = (bid - entry) * sell_qty
                            fee_usd = (entry * sell_qty * 0.001) + (bid * sell_qty * 0.001)
                            net_pnl = gross_pnl - fee_usd
                            database.archive_closed_trade({
                                "id": sp["id"], "bot_name": "SNIPER", "symbol": sym,
                                "entry_price": entry, "exit_price": bid,
                                "qty": sell_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                                "net_pnl": net_pnl, "reason": reason,
                                "entry_time": sp["time_str"], "exit_time": datetime.now(timezone.utc).strftime("%H:%M")
                            })
                            database.delete_sniper_trade(sp["id"])
                            add_log(f"💰 [Sniper] إغلاق {sym} | صافي: {net_pnl:+.3f}$ ({reason})", "sells", "success" if net_pnl > 0 else "danger")
                        else:
                            still_snipers.append(sp)
                    else:
                        database.delete_sniper_trade(sp["id"])
                else:
                    still_snipers.append(sp)

            shared_state["sniper_positions"] = still_snipers

            # متابعة صفقات البوتات الخمسة
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
        
        elif self.path.startswith('/api/scanner'):
            try:
                query = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(query)
                sort_mode = params.get("sort", ["price"])[0]
                min_vol = float(params.get("min_vol", [50000])[0])
                limit = int(params.get("limit", [10])[0])

                url = f"{BASE_URL}/api/v3/ticker/24hr"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=6) as res:
                    tickers = json.loads(res.read().decode('utf-8'))
                    usdt_tickers = [
                        t for t in tickers 
                        if t.get("symbol", "").endswith("USDT") and float(t.get("quoteVolume", 0)) >= min_vol
                    ]
                    if sort_mode == "volume":
                        usdt_tickers.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
                    else:
                        usdt_tickers.sort(key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
                    
                    top_gainers = usdt_tickers[:limit]
                    self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
                    self.wfile.write(json.dumps(top_gainers, ensure_ascii=False).encode('utf-8'))
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
            trades = database.get_closed_trades(bot_name)
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps(trades, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/sniper':
            try:
                with open("sniper.html", "r", encoding="utf-8") as f:
                    html_c = f.read()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(html_c.encode('utf-8'))
            except Exception:
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(b"<h3>sniper.html not found. Please create the file.</h3><a href='/'>Back</a>")

        elif self.path == '/analytics':
            try:
                with open("analytics.html", "r", encoding="utf-8") as f:
                    html_c = f.read()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(html_c.encode('utf-8'))
            except Exception:
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write(b"<h3>analytics.html not found. Please create the file.</h3><a href='/'>Back</a>")

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
                self.wfile.write(b"<h3>dashboard.html not found. Please create the file.</h3>")

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

        if self.path == '/api/sniper_buy':
            sym = data.get("symbol")
            size = float(data.get("size", 10.0))
            o_type = data.get("order_type", "CHASE_LIMIT")
            tp_pct = float(data.get("tp_pct", 0.03))
            sl_pct = float(data.get("sl_pct", 0.015))
            ts_cb = float(data.get("trailing_cb", 0.008))

            bid, ask = get_orderbook(sym)
            if ask:
                q = float(format_quantity(sym, size / ask))
                if o_type == "CHASE_LIMIT":
                    ok, res = execute_smart_chase_order(sym, "BUY", quote_qty=size)
                else:
                    ok, res = place_order(sym, "BUY", qty=q, quote_qty=size, order_type="MARKET")

                if ok:
                    snp_id = f"snp_{int(time.time()*1000)}"
                    time_str = datetime.now(timezone.utc).strftime("%H:%M")
                    snp_trade = {
                        "id": snp_id, "symbol": sym, "entry_price": ask,
                        "highest_price": ask, "qty": q, "tp_pct": tp_pct,
                        "sl_pct": sl_pct, "trailing_cb": ts_cb,
                        "is_break_even": 0, "time_str": time_str
                    }
                    database.insert_sniper_trade(snp_trade)
                    shared_state["sniper_positions"].append(snp_trade)
                    msg = f"🎯 تم قنص {sym} بنجاح عند {ask}$"
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
                        cur_p = bid if bid else sp["entry_price"]
                        gross_pnl = (cur_p - sp["entry_price"]) * sell_qty
                        fee_usd = (sp["entry_price"] * sell_qty * 0.001) + (cur_p * sell_qty * 0.001)
                        net_pnl = gross_pnl - fee_usd
                        database.archive_closed_trade({
                            "id": s_id, "bot_name": "SNIPER", "symbol": sym,
                            "entry_price": sp["entry_price"], "exit_price": cur_p,
                            "qty": sell_qty, "gross_pnl": gross_pnl, "fee_usd": fee_usd,
                            "net_pnl": net_pnl, "reason": "إغلاق يدوي للقناص",
                            "entry_time": sp["time_str"], "exit_time": datetime.now(timezone.utc).strftime("%H:%M")
                        })
                    database.delete_sniper_trade(s_id)
                    add_log(f"🔥 تسييل صفقة قناص {sym}", "sells", "danger")
                    break
            shared_state["sniper_positions"] = [p for p in shared_state.get("sniper_positions", []) if p["id"] != s_id]
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
            self.wfile.write(json.dumps({"msg": "✅ تم إغلاق وتسييل صفقة القناص"}, ensure_ascii=False).encode('utf-8'))

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
