import time
from datetime import datetime, timezone
from exchanges import mexc

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

def calculate_ewo(candles):
    """حساب مؤشر Elliott Wave Oscillator"""
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

def run_ewo_loop(bot_state, get_config_func, add_log_func):
    """الحلقة المستقلة لتشغيل بوت EWO Momentum"""
    bot_name = "EWO_BOT"
    add_log_func(f"[{bot_name}] تم بدء تشغيل خيط استراتيجية EWO Momentum", "info")

    while True:
        try:
            cfg = get_config_func(bot_name)
            status = cfg.get("status", "RUNNING")
            is_paper = bool(cfg.get("paper_trading", 1))
            api_key = cfg.get("api_key", "").strip()
            api_secret = cfg.get("api_secret", "").strip()
            trade_size = float(cfg.get("trade_size_usdt", 10.0))
            stop_loss_pct = float(cfg.get("stop_loss_pct", 0.0049))
            daily_target_coin = float(cfg.get("daily_target_per_coin", 1.50))
            daily_target_port = float(cfg.get("daily_target_portfolio", 5.00))
            max_concurrent = int(cfg.get("max_concurrent_per_coin", 5))

            if status == "STOPPED":
                time.sleep(3)
                continue

            # تصفير الأهداف مع بداية يوم جديد UTC
            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != bot_state["ewo"]["current_day"]:
                bot_state["ewo"]["current_day"] = now_day
                bot_state["ewo"]["daily_pnl_portfolio"] = 0.0
                bot_state["ewo"]["daily_pnl_coins"] = {sym: 0.0 for sym in SYMBOLS}
                add_log_func(f"[{bot_name}] 🌅 يوم جديد ({now_day} UTC) - تصفير الأهداف اليومية", "info")

            port_target_locked = bot_state["ewo"]["daily_pnl_portfolio"] >= daily_target_port

            for sym in SYMBOLS:
                bid, ask = mexc.get_orderbook(sym)
                if bid and ask:
                    bot_state["market_prices"][sym] = {"bid": bid, "ask": ask}

                candles = mexc.fetch_klines(sym, interval="5m", limit=45)
                if not candles or not bid:
                    continue

                e3, e2, e1 = calculate_ewo(candles)
                if e1 is None:
                    continue

                # 1. متابعة الصفقات المفتوحة الخاصة ببوت EWO
                still_open = []
                for pos in bot_state["ewo"]["active_positions"].get(sym, []):
                    entry = pos['entry_price']
                    qty = pos['qty']
                    sl = entry * (1.0 - stop_loss_pct)

                    # إغلاق عند ضرب وقف الخسارة
                    if bid <= sl:
                        ok, res = mexc.place_order(api_key, api_secret, sym, "SELL", qty=qty, is_paper=is_paper)
                        if ok:
                            pnl = (bid - entry) * qty
                            bot_state["ewo"]["virtual_balance"] += (trade_size + pnl)
                            bot_state["ewo"]["daily_pnl_portfolio"] += pnl
                            bot_state["ewo"]["total_realized_pnl"] += pnl
                            bot_state["ewo"]["daily_pnl_coins"][sym] += pnl
                            bot_state["ewo"]["total_trades"] += 1
                            add_log_func(f"[{bot_name}] 🛑 ضرب الوقف لـ {sym} عند {bid}$ (PnL: {pnl:+.3f}$)", "danger")
                        else:
                            still_open.append(pos)

                    # جني الأرباح عند انعكاس مؤشر EWO
                    elif (e2 > 0) and (e1 < e2):
                        ok, res = mexc.place_order(api_key, api_secret, sym, "SELL", qty=qty, is_paper=is_paper)
                        if ok:
                            pnl = (ask - entry) * qty
                            bot_state["ewo"]["virtual_balance"] += (trade_size + pnl)
                            bot_state["ewo"]["daily_pnl_portfolio"] += pnl
                            bot_state["ewo"]["total_realized_pnl"] += pnl
                            bot_state["ewo"]["daily_pnl_coins"][sym] += pnl
                            bot_state["ewo"]["total_trades"] += 1
                            bot_state["ewo"]["winning_trades"] += 1
                            add_log_func(f"[{bot_name}] 🎯 جني أرباح EWO لـ {sym} عند {ask}$ (PnL: {pnl:+.3f}$)", "success")
                        else:
                            still_open.append(pos)
                    else:
                        still_open.append(pos)

                bot_state["ewo"]["active_positions"][sym] = still_open

                # 2. فحص إشارات الشراء الجديدة
                if status == "RUNNING":
                    coin_target_locked = bot_state["ewo"]["daily_pnl_coins"].get(sym, 0.0) >= daily_target_coin
                    sig_rebound = (e1 < 0) and (e1 > e2) and (e2 <= e3)
                    open_count = len(bot_state["ewo"]["active_positions"].get(sym, []))
                    can_open = open_count < max_concurrent

                    avail_bal = bot_state["ewo"]["virtual_balance"] if is_paper else bot_state.get("real_balance", 0.0)
                    has_balance = avail_bal >= trade_size

                    if sig_rebound and can_open and has_balance and not port_target_locked and not coin_target_locked:
                        buy_price = ask
                        raw_qty = trade_size / buy_price
                        qty = float(mexc.format_quantity(sym, raw_qty))
                        
                        if qty > 0:
                            ok, res = mexc.place_order(api_key, api_secret, sym, "BUY", qty=qty, quote_qty=trade_size, is_paper=is_paper)
                            if ok:
                                if is_paper:
                                    bot_state["ewo"]["virtual_balance"] -= trade_size
                                
                                if sym not in bot_state["ewo"]["active_positions"]:
                                    bot_state["ewo"]["active_positions"][sym] = []
                                    
                                bot_state["ewo"]["active_positions"][sym].append({
                                    'entry_price': buy_price,
                                    'qty': qty,
                                    'time': datetime.now(timezone.utc).strftime("%H:%M:%S")
                                })
                                mode_str = "تجريبي" if is_paper else "حقيقي"
                                add_log_func(f"[{bot_name}] 🚀 شراء ({mode_str}) لـ {sym} عند {buy_price}$", "primary")

        except Exception as e:
            add_log_func(f"[{bot_name} Error]: {str(e)}", "warning")

        time.sleep(8)
