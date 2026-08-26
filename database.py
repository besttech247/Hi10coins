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
    CREATE TABLE IF NOT EXISTS bots_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        symbols TEXT DEFAULT 'SOLUSDT, BTCUSDT, ETHUSDT',
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

    default_pass = hashlib.sha256("admin123".encode('utf-8')).hexdigest()
    cursor.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (1, 'admin', ?)", (default_pass,))
    cursor.execute("INSERT OR IGNORE INTO exchange_keys (id, api_key, api_secret) VALUES (1, '', '')")

    # 3 عملات افتراضية ذات سيولة عالية
    default_3_symbols = "SOLUSDT, BTCUSDT, ETHUSDT"

    bots = [
        (1, 'BOT_1', '🤖 Bot 1 (EWO 5m)', default_3_symbols, 50.0, 1, 10.0, '5m', 0.025, 0.012, 0, 'PAUSED'),
        (2, 'BOT_2A', '⚡ Bot 2A (EWO Scalp 15m)', default_3_symbols, 50.0, 1, 10.0, '15m', 0.025, 0.012, 0, 'PAUSED'),
        (3, 'BOT_2B', '⚡ Bot 2B (EWO Swing 1h)', default_3_symbols, 50.0, 1, 10.0, '60m', 0.035, 0.015, 0, 'PAUSED'),
        (4, 'BOT_2C', '⚡ Bot 2C (EWO Custom TF)', default_3_symbols, 50.0, 1, 10.0, '5m', 0.020, 0.010, 0, 'PAUSED'),
        (5, 'BOT_3', '🎯 Bot 3 (Manual Trigger + Trailing)', 'BTCUSDT, ETHUSDT', 50.0, 1, 10.0, '1m', 0.025, 0.012, 1, 'PAUSED')
    ]

    for b in bots:
        cursor.execute("""
        INSERT OR IGNORE INTO bots_config (id, bot_name, display_name, symbols, max_allocation_usdt, max_concurrent_per_coin, trade_size_usdt, timeframe, tp_pct, sl_pct, trailing_stop, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    cursor.execute("SELECT * FROM active_trades")
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
