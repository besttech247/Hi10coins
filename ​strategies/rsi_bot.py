import time
from datetime import datetime, timezone
from exchanges import mexc

SYMBOLS = [
    "NEARUSDT", "AVAXUSDT", "SOLUSDT", "DOGEUSDT", "BTCUSDT",
    "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"
]

def calculate_rsi(candles, period=14):
    """حساب مؤشر القوة النسبية Relative Strength Index"""
    if len(candles) < period + 1:
        return 50.0
    closes = [c['close'] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
        
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def run_rsi_loop(bot_state, get_config_func, add_log_func):
    """الحلقة المستقلة لتشغيل بوت RSI Scalper (الشراء عند التشبع البيعي)"""
    bot_name = "RSI_BOT"
    add_log_func(f"[{bot_name}] تم بدء تشغيل خيط استراتيجية RSI Scalper", "info")

    while True:
        try:
            cfg = get_config_func(bot_name)
            status = cfg.get("status", "PAUSED")
            is_paper = bool(cfg.get("paper_trading", 1))
            api_key = cfg.get("api_key", "").strip()
            api_secret = cfg.get("api_secret", "").strip()
            trade_size = float(cfg.get("trade_size_usdt", 10.0))
            stop_loss_pct = float(cfg.get("stop_loss_pct", 0.008))
            daily_target_coin = float(cfg.get("daily_target_per_coin", 1.50))
            daily_target_port = float(cfg.get("daily_target_portfolio", 5.00))
            max_concurrent = int(cfg.get("max_concurrent_per_coin", 3))

            if status == "STOPPED":
                time.sleep(3)
                continue

            now_day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if now_day != bot_state["rsi"]["current_day"]:
                bot_state["rsi"]["current_day"] = now_day
                bot_state["rsi"]["daily_pnl_portfolio"] = 0.0
                bot_state["rsi"]["daily_pnl_coins"] = {sym: 0.0 for sym in SYMBOLS}
                add_log_func(f"[{bot_name}] 🌅 يوم جديد ({now_day} UTC) - تصفير الأهداف اليومية", "info")

            port_target_locked = bot_state["rsi"]["daily_pnl_portfolio"] >= daily_target_port

            for sym in SYMBOLS:
                bid, ask = mexc.get_orderbook(sym)
                if not bid or not ask:
                    continue

                candles = mexc.fetch_klines(sym, interval="5m", limit=30)
                if not candles:
                    continue

                rsi_val = calculate_rsi(candles, period=14)

                # 1. متابعة الصفقات المفتوحة
                still_open = []
                for pos in bot_state["rsi"]["active_positions"].get(sym, []):
                    entry = pos['entry_price']
                    qty = pos['qty']
                    sl = entry * (1.0 - stop_loss_pct)

                    # وقف الخسارة
                    if bid <= sl:
                        ok, res = mexc.place_order(api_key, api_secret, sym, "SELL", qty=qty, is_paper=is_paper)
                        if ok:
                            pnl = (bid - entry) * qty
                            bot_state["rsi"]["virtual_balance"] += (trade_size + pnl)
                            bot_state["rsi"]["daily_pnl_portfolio"] += pnl
                            bot_state["rsi"]["total_realized_pnl"] += pnl
                            bot_state["rsi"]["daily_pnl_coins"][sym] += pnl
                            bot_state["rsi"]["total_trades"] += 1
                            add_log_func(f"[{bot_name}] 🛑 ضرب الوقف لـ {sym} عند {bid}$ (PnL: {pnl:+.3f}$)", "danger")
                        else:
                            still_open.append(pos)

                    # جني الأرباح عند وصول RSI لمستوى التشبع الشرائي (RSI >= 68) أو ربح سريع +1.2%
                    elif rsi_val >= 68 or (ask >= entry * 1.012):
                        ok, res = mexc.place_order(api_key, api_secret, sym, "SELL", qty=qty, is_paper=is_paper)
                        if ok:
                            pnl = (ask - entry) * qty
                            bot_state["rsi"]["virtual_balance"] += (trade_size + pnl)
                            bot_state["rsi"]["daily_pnl_portfolio"] += pnl
                            bot_state["rsi"]["total_realized_pnl"] += pnl
                            bot_state["rsi"]["daily_pnl_coins"][sym] += pnl
                            bot_state["rsi"]["total_trades"] += 1
                            bot_state["rsi"]["winning_trades"] += 1
                            add_log_func(f"[{bot_name}] 🎯 جني أرباح RSI ({rsi_val:.1f}) لـ {sym} عند {ask}$ (PnL: {pnl:+.3f}$)", "success")
                        else:
                            still_open.append(pos)
                    else:
                        still_open.append(pos)

                bot_state["rsi"]["active_positions"][sym] = still_open

                # 2. الدخول عند تشبع البيع الحاد (RSI <= 28)
                if status == "RUNNING":
                    coin_target_locked = bot_state["rsi"]["daily_pnl_coins"].get(sym, 0.0) >= daily_target_coin
                    open_count = len(bot_state["rsi"]["active_positions"].get(sym, []))
                    can_open = open_count < max_concurrent

                    avail_bal = bot_state["rsi"]["virtual_balance"] if is_paper else bot_state.get("real_balance", 0.0)
                    has_balance = avail_bal >= trade_size

                    # إشارة الدخول: تشبع بيعي
                    if rsi_val <= 28 and can_open and has_balance and not port_target_locked and not coin_target_locked:
                        buy_price = ask
                        raw_qty = trade_size / buy_price
                        qty = float(mexc.format_quantity(sym, raw_qty))

                        if qty > 0:
                            ok, res = mexc.place_order(api_key, api_secret, sym, "BUY", qty=qty, quote_qty=trade_size, is_paper=is_paper)
                            if ok:
                                if is_paper:
                                    bot_state["rsi"]["virtual_balance"] -= trade_size
                                
                                if sym not in bot_state["rsi"]["active_positions"]:
                                    bot_state["rsi"]["active_positions"][sym] = []
                                    
                                bot_state["rsi"]["active_positions"][sym].append({
                                    'entry_price': buy_price,
                                    'qty': qty,
                                    'time': datetime.now(timezone.utc).strftime("%H:%M:%S")
                                })
                                mode_str = "تجريبي" if is_paper else "حقيقي"
                                add_log_func(f"[{bot_name}] ⚡ شراء ارتداد RSI ({rsi_val:.1f}) لـ {sym} عند {buy_price}$", "primary")

        except Exception as e:
            add_log_func(f"[{bot_name} Error]: {str(e)}", "warning")

        time.sleep(10)
.