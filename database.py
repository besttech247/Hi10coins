import sqlite3
import hashlib

DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. إنشاء جدول المستخدمين
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )
    """)

    # 2. إنشاء جدول إعدادات البوتات
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        strategy TEXT NOT NULL,
        api_key TEXT,
        api_secret TEXT,
        capital REAL DEFAULT 500.0,
        trade_size REAL DEFAULT 10.0,
        status TEXT DEFAULT 'STOPPED'
    )
    """)

    # إنشاء مستخدم مدير افتراضي (اسم: admin / كلمة السر: admin123)
    default_pass_hash = hashlib.sha256("admin123".encode('utf-8')).hexdigest()
    cursor.execute("""
    INSERT OR IGNORE INTO users (id, username, password_hash)
    VALUES (1, 'admin', ?)
    """, (default_pass_hash,))

    conn.commit()
    conn.close()
    print("✅ تم إنشاء قاعدة البيانات وحساب المدير الافتراضي بنجاح!")

def verify_user(username, password):
    pass_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ? AND password_hash = ?", (username, pass_hash))
    user = cursor.fetchone()
    conn.close()
    return user is not None

if __name__ == "__main__":
    init_db()
