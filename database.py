import os
import sqlite3
import hashlib
import shutil
import json

LEGACY_DB = "bot_data.db"

DEFAULT_MTF_SETTINGS = {
    "_global": {
        "max_open_positions": 4,
        "be_offset": 0.001,
        "ewo_exit_min_profit": 0.008
    },
    # hierarchy_parent: "off" or a strictly higher TF key (5m/15m/30m/60m/4h/1d). Used by MTFH only.
    "5m":  {"enabled": True,  "trade_size_usdt": 15,  "sl_pct": 0.008, "tp_pct": 0.015, "be_enabled": False, "be_trigger_pct": 0.010, "trail_enabled": False, "trail_trigger_pct": 0.018, "trail_cb_pct": 0.006, "hierarchy_parent": "off"},
    "15m": {"enabled": True,  "trade_size_usdt": 25,  "sl_pct": 0.010, "tp_pct": 0.022, "be_enabled": True,  "be_trigger_pct": 0.012, "trail_enabled": True,  "trail_trigger_pct": 0.018, "trail_cb_pct": 0.006, "hierarchy_parent": "off"},
    "30m": {"enabled": True,  "trade_size_usdt": 35,  "sl_pct": 0.012, "tp_pct": 0.028, "be_enabled": True,  "be_trigger_pct": 0.015, "trail_enabled": True,  "trail_trigger_pct": 0.022, "trail_cb_pct": 0.007, "hierarchy_parent": "off"},
    "60m": {"enabled": True,  "trade_size_usdt": 50,  "sl_pct": 0.014, "tp_pct": 0.035, "be_enabled": True,  "be_trigger_pct": 0.018, "trail_enabled": True,  "trail_trigger_pct": 0.028, "trail_cb_pct": 0.008, "hierarchy_parent": "off"},
    "4h":  {"enabled": True,  "trade_size_usdt": 70,  "sl_pct": 0.018, "tp_pct": 0.045, "be_enabled": True,  "be_trigger_pct": 0.022, "trail_enabled": True,  "trail_trigger_pct": 0.035, "trail_cb_pct": 0.010, "hierarchy_parent": "off"},
    "1d":  {"enabled": False, "trade_size_usdt": 100, "sl_pct": 0.025, "tp_pct": 0.060, "be_enabled": True,  "be_trigger_pct": 0.030, "trail_enabled": True,  "trail_trigger_pct": 0.045, "trail_cb_pct": 0.012, "hierarchy_parent": "off"}
}

MTF_TF_ORDER = ["5m", "15m", "30m", "60m", "4h", "1d"]
MTF_TF_LABELS = {"5m": "5m", "15m": "15m", "30m": "30m", "60m": "1h", "4h": "4h", "1d": "1d"}


def normalize_hierarchy_parent(entry_tf, parent):
    """Allow only off or a strictly higher timeframe than entry_tf."""
    raw = str(parent or "off").strip().lower()
    if raw in ("", "off", "none", "disabled", "0"):
        return "off"
    if raw == "1h":
        raw = "60m"
    try:
        idx = MTF_TF_ORDER.index(entry_tf)
    except ValueError:
        return "off"
    higher = MTF_TF_ORDER[idx + 1:]
    return raw if raw in higher else "off"


def _resolve_db_file():
    """Use DB_PATH or /data on Railway Volume; fall back to local file if needed."""
    preferred = os.environ.get("DB_PATH", "/data/bot_data.db")
    db_dir = os.path.dirname(preferred) or "."
    try:
        os.makedirs(db_dir, exist_ok=True)
        test_path = os.path.join(db_dir, ".db_write_test")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_path)
        return preferred
    except OSError:
        return LEGACY_DB


DB_FILE = _resolve_db_file()


def _ensure_db_location():
    """Create DB directory and migrate a local bot_data.db once if present."""
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    if (
        DB_FILE != LEGACY_DB
        and not os.path.exists(DB_FILE)
        and os.path.exists(LEGACY_DB)
        and os.path.getsize(LEGACY_DB) > 0
    ):
        shutil.copy2(LEGACY_DB, DB_FILE)


def _ensure_column(cursor, table, column, col_def):
    cursor.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cursor.fetchall()]
    if column not in cols:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def parse_mtf_settings(raw):
    settings = json.loads(json.dumps(DEFAULT_MTF_SETTINGS))
    if not raw:
        for tf in MTF_TF_ORDER:
            if tf in settings:
                settings[tf]["hierarchy_parent"] = normalize_hierarchy_parent(tf, settings[tf].get("hierarchy_parent", "off"))
        return settings
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return settings
    if isinstance(data, dict):
        if "_global" in data and isinstance(data["_global"], dict):
            settings["_global"].update(data["_global"])
        for tf, cfg in data.items():
            if tf == "_global" or not isinstance(cfg, dict):
                continue
            if tf not in settings:
                settings[tf] = {}
            settings[tf].update(cfg)
    for tf in MTF_TF_ORDER:
        if tf in settings and isinstance(settings[tf], dict):
            settings[tf]["hierarchy_parent"] = normalize_hierarchy_parent(tf, settings[tf].get("hierarchy_parent", "off"))
    return settings


def dump_mtf_settings(settings):
    return json.dumps(settings if settings else DEFAULT_MTF_SETTINGS, ensure_ascii=False)


def init_db():
    _ensure_db_location()
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
        time_str TEXT NOT NULL,
        timeframe TEXT DEFAULT '',
        meta_json TEXT DEFAULT '{}'
    )
    """)

    _ensure_column(cursor, "bots_config", "mtf_settings", "TEXT DEFAULT ''")
    _ensure_column(cursor, "bots_config", "daily_profit_target", "REAL DEFAULT 5.0")
    _ensure_column(cursor, "bots_config", "daily_loss_limit", "REAL DEFAULT 5.0")
    _ensure_column(cursor, "active_trades", "timeframe", "TEXT DEFAULT ''")
    _ensure_column(cursor, "active_trades", "meta_json", "TEXT DEFAULT '{}'")
    _ensure_column(cursor, "sniper_trades", "entry_fee_rate", "REAL DEFAULT 0.001")

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

    # Remove deprecated bots before seeding replacements.
    cursor.execute("DELETE FROM bots_config WHERE bot_name = 'BOT_3'")
    cursor.execute("DELETE FROM active_trades WHERE bot_name = 'BOT_3'")
    for deprecated in ("BOT_2A", "BOT_2B", "BOT_2C"):
        cursor.execute("DELETE FROM bots_config WHERE bot_name = ?", (deprecated,))
        cursor.execute("DELETE FROM active_trades WHERE bot_name = ?", (deprecated,))

    # Migrate legacy single BOT_X -> BOT_X1 safely
    cursor.execute("SELECT id FROM bots_config WHERE bot_name = 'BOT_X1'")
    has_x1 = cursor.fetchone() is not None
    cursor.execute("SELECT id FROM bots_config WHERE bot_name = 'BOT_X'")
    has_x = cursor.fetchone() is not None
    if has_x and not has_x1:
        cursor.execute("UPDATE bots_config SET bot_name = 'BOT_X1', display_name = '🧪 Bot X1 (سريع 5m)' WHERE bot_name = 'BOT_X'")
    elif has_x and has_x1:
        cursor.execute("DELETE FROM bots_config WHERE bot_name = 'BOT_X'")
    cursor.execute("UPDATE active_trades SET bot_name = 'BOT_X1' WHERE bot_name = 'BOT_X'")
    cursor.execute("UPDATE closed_trades SET bot_name = 'BOT_X1' WHERE bot_name = 'BOT_X'")

    bots = [
        (1, 'BOT_1', '🤖 Bot 1 (EWO 5m)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '5m', 0.025, 0.012, 0, 'PAUSED'),
        (5, 'BOT_X1', '🧪 Bot X1 (سريع 5m)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '5m', 0.015, 0.008, 1, 'PAUSED'),
        (6, 'BOT_X2', '🧪 Bot X2 (قياسي 15m)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '15m', 0.025, 0.010, 1, 'PAUSED'),
        (7, 'BOT_X3', '🧪 Bot X3 (أوسع 15m)', default_3_symbols, 'CHASE_LIMIT', 50.0, 1, 10.0, '15m', 0.035, 0.012, 1, 'PAUSED'),
        (8, 'BOT_EWO_MTF', '📐 Bot EWO MTF', default_3_symbols, 'CHASE_LIMIT', 300.0, 2, 15.0, '15m', 0.022, 0.010, 1, 'PAUSED'),
        (9, 'BOT_EWO_MTFH', '📐 Bot EWO MTFH', default_3_symbols, 'CHASE_LIMIT', 300.0, 2, 15.0, '15m', 0.022, 0.010, 1, 'PAUSED')
    ]

    for b in bots:
        cursor.execute("""
        INSERT OR IGNORE INTO bots_config (id, bot_name, display_name, symbols, order_exec_type, max_allocation_usdt, max_concurrent_per_coin, trade_size_usdt, timeframe, tp_pct, sl_pct, trailing_stop, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, b)

    x_defaults = [
        ('BOT_X1', '🧪 Bot X1 (سريع 5m)', '5m', 0.015, 0.008, 0.005),
        ('BOT_X2', '🧪 Bot X2 (قياسي 15m)', '15m', 0.025, 0.010, 0.006),
        ('BOT_X3', '🧪 Bot X3 (أوسع 15m)', '15m', 0.035, 0.012, 0.008),
    ]
    for name, display, tf, tp, sl, cb in x_defaults:
        cursor.execute("SELECT id FROM bots_config WHERE bot_name = ?", (name,))
        if cursor.fetchone() is None:
            cursor.execute("""
            INSERT INTO bots_config (bot_name, display_name, symbols, order_exec_type, max_allocation_usdt, max_concurrent_per_coin, trade_size_usdt, timeframe, tp_pct, sl_pct, trailing_stop, trailing_cb, status)
            VALUES (?, ?, ?, 'CHASE_LIMIT', 50.0, 1, 10.0, ?, ?, ?, 1, ?, 'PAUSED')
            """, (name, display, default_3_symbols, tf, tp, sl, cb))
        cursor.execute("""
        UPDATE bots_config
        SET display_name = ?,
            trailing_stop = 1,
            trailing_cb = COALESCE(trailing_cb, ?)
        WHERE bot_name = ?
        """, (display, cb, name))

    cursor.execute("DELETE FROM bots_config WHERE bot_name = 'BOT_X'")

    # Ensure MTF / MTFH bots exist with defaults
    mtf_bots = [
        ('BOT_EWO_MTF', '📐 Bot EWO MTF'),
        ('BOT_EWO_MTFH', '📐 Bot EWO MTFH'),
    ]
    for bot_name, display in mtf_bots:
        cursor.execute("SELECT id FROM bots_config WHERE bot_name = ?", (bot_name,))
        if cursor.fetchone() is None:
            cursor.execute("""
            INSERT INTO bots_config (bot_name, display_name, symbols, order_exec_type, max_allocation_usdt, max_concurrent_per_coin, trade_size_usdt, timeframe, tp_pct, sl_pct, trailing_stop, trailing_cb, status, mtf_settings)
            VALUES (?, ?, ?, 'CHASE_LIMIT', 300.0, 2, 15.0, '15m', 0.022, 0.010, 1, 0.006, 'PAUSED', ?)
            """, (bot_name, display, default_3_symbols, dump_mtf_settings(DEFAULT_MTF_SETTINGS)))
        else:
            cursor.execute("""
            UPDATE bots_config
            SET display_name = ?,
                max_allocation_usdt = CASE WHEN max_allocation_usdt < 100 THEN 300.0 ELSE max_allocation_usdt END,
                max_concurrent_per_coin = CASE WHEN max_concurrent_per_coin < 2 THEN 2 ELSE max_concurrent_per_coin END,
                mtf_settings = CASE WHEN mtf_settings IS NULL OR mtf_settings = '' THEN ? ELSE mtf_settings END
            WHERE bot_name = ?
            """, (display, dump_mtf_settings(DEFAULT_MTF_SETTINGS), bot_name))

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
    if not row:
        return {}
    cfg = dict(row)
    if bot_name in ("BOT_EWO_MTF", "BOT_EWO_MTFH"):
        cfg["mtf_settings"] = parse_mtf_settings(cfg.get("mtf_settings"))
    return cfg

def update_bot_config(bot_name, updates):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    clean = dict(updates)
    if "mtf_settings" in clean and not isinstance(clean["mtf_settings"], str):
        clean["mtf_settings"] = dump_mtf_settings(clean["mtf_settings"])
    fields = [f"{k} = ?" for k in clean.keys()]
    values = list(clean.values())
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
    out = []
    for r in rows:
        d = dict(r)
        meta = {}
        try:
            meta = json.loads(d.get("meta_json") or "{}")
        except Exception:
            meta = {}
        d["meta"] = meta if isinstance(meta, dict) else {}
        out.append(d)
    return out

def insert_active_trade(trade):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    meta = trade.get("meta") or {}
    if trade.get("meta_json"):
        meta_json = trade.get("meta_json")
    else:
        meta_json = json.dumps(meta, ensure_ascii=False)
    cursor.execute("""
    INSERT OR REPLACE INTO active_trades (id, bot_name, symbol, entry_price, highest_price, qty, tp_pct, sl_pct, time_str, timeframe, meta_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["id"], trade["bot_name"], trade["symbol"], trade["entry_price"],
        trade.get("highest_price", trade["entry_price"]), trade["qty"],
        trade.get("tp_pct", 0.025), trade.get("sl_pct", 0.012), trade["time_str"],
        trade.get("timeframe", ""), meta_json
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
    INSERT OR REPLACE INTO sniper_trades (id, sniper_profile, symbol, entry_price, highest_price, qty, orig_qty, tp1_pct, tp2_pct, sl_pct, trailing_cb, tp1_hit, time_str, entry_fee_rate)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["id"], trade.get("sniper_profile", "SNIPER_1"), trade["symbol"], trade["entry_price"],
        trade.get("highest_price", trade["entry_price"]), trade["qty"], trade["qty"],
        trade.get("tp1_pct", 0.015), trade.get("tp2_pct", 0.030), trade.get("sl_pct", 0.010),
        trade.get("trailing_cb", 0.006), 0, trade["time_str"],
        float(trade.get("entry_fee_rate", 0.001))
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
