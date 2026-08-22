import sqlite3
import hashlib

DB_FILE = "bot_data.db"

def init_db():
    """تهيئة قاعدة البيانات والجداول وحساب المدير تلقائياً"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # جدول المستخدمين لحماية لوحة التحكم
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )
    """)

    # جدول إعدادات البوتات المستقلة
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT UNIQUE NOT NULL,
        exchange TEXT DEFAULT 'MEXC',
        api_key TEXT DEFAULT '',
        api_secret TEXT DEFAULT '',
        paper_trading INTEGER DEFAULT 1,
        initial_capital REAL DEFAULT 500.0,
        trade_size_usdt REAL DEFAULT 10.0,
        max_concurrent_per_coin INTEGER DEFAULT 5,
        stop_loss_pct REAL DEFAULT 0.0049,
        daily_target_per_coin REAL DEFAULT 1.50,
        daily_target_portfolio REAL DEFAULT 5.00,
        status TEXT DEFAULT 'RUNNING'
    )
    """)

    # جدول تاريخ وسجل الصفقات المفتوحة والمغلقة
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trade_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        entry_price REAL NOT NULL,
        exit_price REAL,
        qty REAL NOT NULL,
        pnl REAL DEFAULT 0.0,
        status TEXT DEFAULT 'OPEN',
        entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        exit_time TIMESTAMP
    )
    """)

    # حساب المدير الافتراضي: admin / admin123
    default_pass = hashlib.sha256("admin123".encode('utf-8')).hexdigest()
    cursor.execute("""
    INSERT OR IGNORE INTO users (id, username, password_hash)
    VALUES (1, 'admin', ?)
    """, (default_pass,))

    # إعدادات افتراضية للبوت الأول (EWO Momentum) والبوت الثاني (RSI Scalper)
    cursor.execute("""
    INSERT OR IGNORE INTO bots_config (id, bot_name, exchange, paper_trading, trade_size_usdt, initial_capital, status)
    VALUES (1, 'EWO_BOT', 'MEXC', 1, 10.0, 500.0, 'RUNNING')
    """)
    cursor.execute("""
    INSERT OR IGNORE INTO bots_config (id, bot_name, exchange, paper_trading, trade_size_usdt, initial_capital, status)
    VALUES (2, 'RSI_BOT', 'MEXC', 1, 10.0, 500.0, 'PAUSED')
    """)

    conn.commit()
    conn.close()

def get_bot_config(bot_name):
    """جلب إعدادات بوت محدد"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bots_config WHERE bot_name = ?", (bot_name,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}

def update_bot_config(bot_name, updates):
    """تحديث إعدادات البوت في قاعدة البيانات"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    fields = [f"{k} = ?" for k in updates.keys()]
    values = list(updates.values())
    values.append(bot_name)
    cursor.execute(f"UPDATE bots_config SET {', '.join(fields)} WHERE bot_name = ?", values)
    conn.commit()
    conn.close()

def verify_user(username, password):
    """التحقق من بيانات تسجيل الدخول"""
    pass_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ? AND password_hash = ?", (username, pass_hash))
    user = cursor.fetchone()
    conn.close()
    return user is not None
