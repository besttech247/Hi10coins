import urllib.request
import urllib.parse
import urllib.error
import ssl
import time
import hmac
import hashlib
import json
import math

BASE_URL = "https://api.mexc.com"
ssl_ctx = ssl._create_unverified_context()

# خريطة دقة الكسور العشرية لكل عملة (يتم تحديثها تلقائياً من المنصة)
PRECISION_MAP = {
    "NEARUSDT": 2, "AVAXUSDT": 2, "SOLUSDT": 2, "DOGEUSDT": 0, "BTCUSDT": 4,
    "ETHUSDT": 4, "BNBUSDT": 3, "XRPUSDT": 1, "ADAUSDT": 1, "LINKUSDT": 2
}

def sign_query(query_string, secret):
    """توليد التوقيع الرقمي HMAC-SHA256 لأوامر MEXC"""
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def private_request(api_key, api_secret, endpoint, method="GET", params=None):
    """إرسال طلب خاص وموقع إلى MEXC API"""
    if not api_key or not api_secret:
        return False, "مفاتيح API مفقودة أو غير محددة"
    if params is None:
        params = {}
    
    params["timestamp"] = int(time.time() * 1000)
    query_string = urllib.parse.urlencode(params)
    signature = sign_query(query_string, api_secret)
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    
    headers = {
        "X-MEXC-APIKEY": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as res:
            return True, json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return False, f"خطأ HTTP {e.code}: {e.read().decode('utf-8')}"
    except Exception as e:
        return False, f"فشل الاتصال: {str(e)}"

def fetch_exchange_precisions():
    """جلب قواعد دقة الكسور العشرية المعتمدة من MEXC لجميع العملات"""
    try:
        url = f"{BASE_URL}/api/v3/exchangeInfo"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as res:
            data = json.loads(res.read().decode('utf-8'))
            for s in data.get("symbols", []):
                sym = s.get("symbol")
                prec = s.get("baseAssetPrecision", 2)
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step = float(f.get("stepSize", "0.01"))
                        if step > 0:
                            prec = max(0, int(round(-math.log10(step))))
                PRECISION_MAP[sym] = prec
            return True
    except Exception:
        return False

def format_quantity(symbol, qty):
    """تقريب الكمية للأسفل بحسب دقة المنصة لتفادي خطأ 400 Bad Request"""
    prec = PRECISION_MAP.get(symbol, 2)
    factor = 10 ** prec
    truncated = math.floor(qty * factor) / factor
    return f"{int(truncated)}" if prec == 0 else f"{truncated:.{prec}f}"

def get_orderbook(symbol):
    """جلب سعر العرض والطلب اللحظي"""
    try:
        url = f"{BASE_URL}/api/v3/ticker/bookTicker?symbol={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5) as res:
            d = json.loads(res.read().decode('utf-8'))
            return float(d['bidPrice']), float(d['askPrice'])
    except Exception:
        return None, None

def fetch_klines(symbol, interval="5m", limit=45):
    """جلب بيانات الشموع اليابانية"""
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=7) as res:
            data = json.loads(res.read().decode('utf-8'))
            return [{'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4])} for r in data]
    except Exception:
        return []

def get_account_balances(api_key, api_secret):
    """قراءة كافة أرصدة المحفظة الحالية في MEXC"""
    ok, data = private_request(api_key, api_secret, "/api/v3/account", method="GET")
    if ok and "balances" in data:
        assets = []
        for a in data["balances"]:
            free = float(a["free"])
            locked = float(a["locked"])
            total = free + locked
            if total > 0.00001:
                assets.append({"asset": a["asset"], "free": free, "locked": locked, "total": total})
        return True, assets
    return False, data

def place_order(api_key, api_secret, symbol, side, qty=None, quote_qty=None, is_paper=False):
    """تنفيذ أمر بيع أو شراء فوري بسعر السوق"""
    if is_paper:
        return True, {"status": "FILLED", "orderId": f"PAPER_{int(time.time()*1000)}"}
    
    params = {"symbol": symbol, "side": side.upper(), "type": "MARKET"}
    if side.upper() == "BUY" and quote_qty:
        params["quoteOrderQty"] = f"{quote_qty:.2f}"
    elif qty:
        params["quantity"] = format_quantity(symbol, qty)
    else:
        return False, "يجب تحديد الكمية أو القيمة"

    return private_request(api_key, api_secret, "/api/v3/order", method="POST", params=params)
