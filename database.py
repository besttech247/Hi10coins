import sqlite3
import hashlib

DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. جدول تسجيل الدخول
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )
    """)

    # 2. جدول إعدادات المنصة الموحدة (MEXC)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exchange_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        api_key TEXT DEFAULT '',
        api_secret TEXT DEFAULT ''
    )
    """)

    # 3. جدول إعدادات البوتات الثلاثة
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        paper_trading INTEGER DEFAULT 1,
        trade_size_usdt REAL DEFAULT 10.0,
        stop_loss_pct REAL DEFAULT 0.0049,
        status TEXT DEFAULT 'RUNNING'
    )
    """)

    # إنشاء مستخدم المدير الافتراضي (admin / admin123)
    default_pass = hashlib.sha256("admin123".encode('utf-8')).hexdigest()
    cursor.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (1, 'admin', ?)", (default_pass,))

    # إدخال سجل مفاتيح MEXC الافتراضي
    cursor.execute("INSERT OR IGNORE INTO exchange_keys (id, api_key, api_secret) VALUES (1, '', '')")

    # تهيئة البوتات الثلاثة
    cursor.execute("INSERT OR IGNORE INTO bots_config (id, bot_name, display_name, paper_trading, trade_size_usdt, status) VALUES (1, 'BOT_1', '🤖 EWO Momentum Bot', 1, 10.0, 'RUNNING')")
    cursor.execute("INSERT OR IGNORE INTO bots_config (id, bot_name, display_name, paper_trading, trade_size_usdt, status) VALUES (2, 'BOT_2', '⚡ Bot 2 (قريباً)', 1, 10.0, 'PAUSED')")
    cursor.execute("INSERT OR IGNORE INTO bots_config (id, bot_name, display_name, paper_trading, trade_size_usdt, status) VALUES (3, 'BOT_3', '📈 Bot 3 (قريباً)', 1, 10.0, 'PAUSED')")

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

def verify_user(username, password):
    pass_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ? AND password_hash = ?", (username, pass_hash))
    user = cursor.fetchone()
    conn.close()
    return user is not None
