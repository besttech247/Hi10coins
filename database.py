import sqlite3
import hashlib

DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exchange_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        api_key TEXT DEFAULT '',
        api_secret TEXT DEFAULT ''
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS global_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chase_timeout INTEGER DEFAULT 12,
        chase_interval REAL DEFAULT 2.0
    )
    """)

    # إعدادات بروفايلات القناص (قناص 1 سريع وقناص 2 متوسط)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sniper_profiles (
        id TEXT PRIMARY KEY,
        profile_name TEXT NOT NULL,
        trade_size REAL DEFAULT 10.0,
        order_type TEXT DEFAULT 'CHASE_LIMIT',
        tp1_pct REAL DEFAULT 0.015,
        tp2_pct REAL DEFAULT 0.030,
        sl_pct REAL DEFAULT 0.010,
        trailing_cb REAL DEFAULT 0.006
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        symbols TEXT DEFAULT 'SOLUSDT, BTCUSDT, ETHUSDT',
        order_exec_type TEXT DEFAULT 'CHASE_LIMIT',
        max_allocation_usdt REAL DEFAULT 50.0,
        max_concurrent_per_coin INTEGER DEFAULT 1,
        trade_size_usdt REAL DEFAULT 10.0,
        timeframe TEXT DEFAULT '15m',
        tp_pct REAL DEFAULT 0.025,
        sl_pct REAL DEFAULT 0.012,
        trailing_stop INTEGER DEFAULT 0,
        trailing_cb REAL DEFAULT 0.005,
        status TEXT DEFAULT 'PAUSED'
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS active_trades (
        id TEXT PRIMARY KEY,
        bot_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        entry_price REAL NOT NULL,
        highest_price REAL NOT NULL,
        qty REAL NOT NULL,
        tp_pct REAL DEFAULT 0.025,
        sl_pct REAL DEFAULT 0.012,
        time_str TEXT NOT NULL
    )
    """)

    # جدول صفقات القناص مع التاق الخاص بالبروفايل
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sniper_trades (
        id TEXT PRIMARY KEY,
        sniper_profile TEXT DEFAULT 'SNIPER_1',
        symbol TEXT NOT NULL,
        entry_price REAL NOT NULL,
        highest_price REAL NOT NULL,
        qty REAL NOT NULL,
        orig_qty REAL NOT NULL,
        tp1_pct REAL DEFAULT 0.015,
        tp2_pct REAL DEFAULT 0.030,
        sl_pct REAL DEFAULT 0.010,
        trailing_cb REAL DEFAULT 0.006,
        tp1_hit INTEGER DEFAULT 0,
        time_str TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS closed_trades (
        id TEXT PRIMARY KEY,
        bot_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        entry_price REAL NOT NULL,
        exit_price REAL NOT NULL,
        qty REAL NOT NULL,
        gross_pnl REAL NOT NULL,
        fee_usd REAL NOT NULL,
        net_pnl REAL NOT NULL,
        reason TEXT NOT NULL,
        entry_time TEXT NOT NULL,
        exit_time TEXT NOT NULL
    )
    """)

    default_pass = hashlib.sha256("admin123".encode('utf-8')).hexdigest()
    cursor.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (1, 'admin', ?)", (default_pass,))
    cursor.execute("INSERT OR IGNORE INTO exchange_keys (id, api_key, api_secret) VALUES (1, '', '')")
    cursor.execute("INSERT OR IGNORE INTO global_settings (id, chase_timeout, chase_interval) VALUES (1, 12, 2.0)")

    # البروفايلات الافتراضية للقناص
    cursor.execute("""
    INSERT OR IGNORE INTO sniper_profiles (id, profile_name, trade_size, order_type, tp1_pct, tp2_pct, sl_pct, trailing_cb)
    VALUES ('SNIPER_1', '🎯 قناص 1 (سريع)', 10.0, 'CHASE_LIMIT', 0.015, 0.030, 0.010, 0.006)
    """)
    cursor.execute("""
    INSERT OR IGNORE INTO sniper_profiles (id, profile_name, trade_size, order_type, tp1_pct, tp2_pct, sl_pct, trailing_cb)
    VALUES ('SNIPER_2', '🌊 قناص 2 (متوسط)', 15.0, 'CHASE_LIMIT', 0.035, 0.070, 0.018, 0.010)
    """)

    default_3_symbols = "SOLUSDT, BTCUSDT, ETHUSDT"
    bots = [
        (1, 'BOT_1', '🤖 Bot 1 (EWO 5m)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '5m', 0.025, 0.012, 0, 'PAUSED'),
        (2, 'BOT_2A', '⚡ Bot 2A (Scalp 15m)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '15m', 0.025, 0.012, 0, 'PAUSED'),
        (3, 'BOT_2B', '⚡ Bot 2B (Swing 1h)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '60m', 0.035, 0.015, 0, 'PAUSED'),
        (4, 'BOT_2C', '⚡ Bot 2C (Custom TF)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '5m', 0.020, 0.010, 0, 'PAUSED'),
        (5, 'BOT_3', '🎯 Bot 3 (Manual Trigger)', 'BTCUSDT, ETHUSDT', 'CHASE_LIMIT', 50.0, 1, 10.0, '1m', 0.025, 0.012, 1, 'PAUSED')
    ]

    for b in bots:
        cursor.execute("""
        INSERT OR IGNORE INTO bots_config (id, bot_name, display_name, symbols, order_exec_type, max_allocation_usdt, max_concurrent_per_coin, trade_size_usdt, timeframe, tp_pct, sl_pct, trailing_stop, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, b)

    conn.commit()
    conn.close()

def get_keys():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT api_key, api_secret FROM exchange_keys WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {"api_key": "", "api_secret": ""}

def save_keys(api_key, api_secret):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE exchange_keys SET api_key = ?, api_secret = ? WHERE id = 1", (api_key.strip(), api_secret.strip()))
    conn.commit()
    conn.close()

def get_global_settings():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT chase_timeout, chase_interval FROM global_settings WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {"chase_timeout": 12, "chase_interval": 2.0}

def save_global_settings(timeout, interval):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE global_settings SET chase_timeout = ?, chase_interval = ? WHERE id = 1", (int(timeout), float(interval)))
    conn.commit()
    conn.close()

def get_sniper_profiles():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sniper_profiles")
    rows = cursor.fetchall()
    conn.close()
    return {r["id"]: dict(r) for r in rows}

def save_sniper_profile(profile_id, updates):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    fields = [f"{k} = ?" for k in updates.keys()]
    values = list(updates.values())
    values.append(profile_id)
    cursor.execute(f"UPDATE sniper_profiles SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()

def get_bot_config(bot_name):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bots_config WHERE bot_name = ?", (bot_name,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}

def update_bot_config(bot_name, updates):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    fields = [f"{k} = ?" for k in updates.keys()]
    values = list(updates.values())
    values.append(bot_name)
    cursor.execute(f"UPDATE bots_config SET {', '.join(fields)} WHERE bot_name = ?", values)
    conn.commit()
    conn.close()

def load_all_active_trades():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM active_trades ORDER BY time_str DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def insert_active_trade(trade):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO active_trades (id, bot_name, symbol, entry_price, highest_price, qty, tp_pct, sl_pct, time_str)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["id"], trade["bot_name"], trade["symbol"], trade["entry_price"],
        trade.get("highest_price", trade["entry_price"]), trade["qty"],
        trade.get("tp_pct", 0.025), trade.get("sl_pct", 0.012), trade["time_str"]
    ))
    conn.commit()
    conn.close()

def update_active_trade(trade_id, updates):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    fields = [f"{k} = ?" for k in updates.keys()]
    values = list(updates.values())
    values.append(trade_id)
    cursor.execute(f"UPDATE active_trades SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()

def delete_active_trade(trade_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_trades WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()

def load_sniper_trades():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sniper_trades ORDER BY time_str DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def insert_sniper_trade(trade):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO sniper_trades (id, sniper_profile, symbol, entry_price, highest_price, qty, orig_qty, tp1_pct, tp2_pct, sl_pct, trailing_cb, tp1_hit, time_str)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["id"], trade.get("sniper_profile", "SNIPER_1"), trade["symbol"], trade["entry_price"],
        trade.get("highest_price", trade["entry_price"]), trade["qty"], trade["qty"],
        trade.get("tp1_pct", 0.015), trade.get("tp2_pct", 0.030), trade.get("sl_pct", 0.010),
        trade.get("trailing_cb", 0.006), 0, trade["time_str"]
    ))
    conn.commit()
    conn.close()

def update_sniper_trade(trade_id, updates):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    fields = [f"{k} = ?" for k in updates.keys()]
    values = list(updates.values())
    values.append(trade_id)
    cursor.execute(f"UPDATE sniper_trades SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()

def delete_sniper_trade(trade_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sniper_trades WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()

def archive_closed_trade(trade):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO closed_trades (id, bot_name, symbol, entry_price, exit_price, qty, gross_pnl, fee_usd, net_pnl, reason, entry_time, exit_time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["id"], trade["bot_name"], trade["symbol"], trade["entry_price"],
        trade["exit_price"], trade["qty"], trade["gross_pnl"], trade["fee_usd"],
        trade["net_pnl"], trade["reason"], trade["entry_time"], trade["exit_time"]
    ))
    conn.commit()
    conn.close()

def get_closed_trades(bot_name=None, limit=70):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if bot_name:
        if bot_name == "SNIPER_ALL":
            cursor.execute("SELECT * FROM closed_trades WHERE bot_name LIKE 'SNIPER%' ORDER BY exit_time DESC LIMIT ?", (limit,))
        else:
            cursor.execute("SELECT * FROM closed_trades WHERE bot_name = ? ORDER BY exit_time DESC LIMIT ?", (bot_name, limit))
    else:
        cursor.execute("SELECT * FROM closed_trades ORDER BY exit_time DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def verify_user(username, password):
    pass_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ? AND password_hash = ?", (username, pass_hash))
    user = cursor.fetchone()
    conn.close()
    return user is not None

def change_password(new_password):
    pass_hash = hashlib.sha256(new_password.encode('utf-8')).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password_hash = ? WHERE id = 1", (pass_hash,))
    conn.commit()
    conn.close()
    return True
