# app.py
import os
import math
import time
import requests
from functools import wraps
from datetime import datetime, timezone, timedelta
from flask import Flask, request, Response, render_template_string, jsonify

app = Flask(__name__)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "QuarterBand/quality-prob-seasonality"})

# =========================
# Config (env-tunable)
# =========================
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = os.getenv("QB_PASS", "password")
COINBASE_EXCHANGE_API = "https://api.exchange.coinbase.com"

# Price window (starts here; can widen automatically)
PRICE_MIN = float(os.getenv("PRICE_MIN", "0.10"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "0.25"))

# Ensure at least N candidates; widen upper bound until we have them
TOP_K         = int(os.getenv("TOP_K", "5"))     # how many to display
MIN_COUNT     = int(os.getenv("MIN_COUNT", "5")) # minimum to find before ranking
EXPAND_STEP   = float(os.getenv("EXPAND_STEP", "0.05"))
MAX_PRICE_CAP = float(os.getenv("MAX_PRICE_CAP", "1.00"))

# Quality filter knobs
MIN_24H_DOLLAR_VOL = float(os.getenv("MIN_24H_DOLLAR_VOL", "20000000"))  # $20M default
MAX_SPREAD_PCT     = float(os.getenv("MAX_SPREAD_PCT", "0.008"))         # 0.8% default
SYMBOL_WHITELIST = {x.strip().upper() for x in os.getenv("SYMBOL_WHITELIST", "").split(",") if x.strip()}
SYMBOL_BLACKLIST = {x.strip().upper() for x in os.getenv(
    "SYMBOL_BLACKLIST",
    "USLESS,COOKIE,PNUT,PEPE,SHIB,FLOKI,WIF,BONK"
).split(",") if x.strip()}

# Link behavior: "price" | "advanced" | "both"
LINK_TARGET = os.getenv("LINK_TARGET", "price").lower()
if LINK_TARGET not in {"price", "advanced", "both"}:
    LINK_TARGET = "price"

# Seasonality config
SEASONALITY_DAYS = int(os.getenv("SEASONALITY_DAYS", "30"))  # lookback days
SEASONALITY_GRAN = 3600  # hourly candles

# =========================
# Basic Auth
# =========================
def check_auth(username, password):
    return username == QB_USER and password == QB_PASS

def authenticate():
    return Response("Authentication required", 401,
        {"WWW-Authenticate": 'Basic realm="QuarterBand"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# =========================
# Coinbase helpers
# =========================
def cb_get(path, **params):
    url = f"{COINBASE_EXCHANGE_API}{path}"
    r = SESSION.get(url, params=params, timeout=12)
    r.raise_for_status()
    return r.json()

def list_usd_products():
    """Active USD markets."""
    products = cb_get("/products")
    out = []
    for p in products:
        if (
            p.get("quote_currency") == "USD"
            and p.get("status") == "online"
            and not p.get("trading_disabled", False)
        ):
            out.append({"id": p["id"], "base": p["base_currency"]})
    return out

def fetch_snapshot(pair):
    """Return price + 24h stats + bid/ask spread for 'XYZ-USD'."""
    t = cb_get(f"/products/{pair}/ticker")   # price, bid, ask
    s = cb_get(f"/products/{pair}/stats")    # open, high, low, volume (24h)

    def f(d, k, dflt=0.0):
        try:
            return float(d.get(k, dflt))
        except Exception:
            return dflt

    price = f(t, "price")
    bid   = f(t, "bid")
    ask   = f(t, "ask")
    mid   = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (price or 0.0)
    spread_pct = ((ask - bid) / mid) if (bid > 0 and ask > 0 and mid > 0) else 1.0  # 100% if unknown

    return {
        "pair": pair,
        "price": price,
        "open":  f(s, "open"),
        "high":  f(s, "high"),
        "low":   f(s, "low"),
        "volume": f(s, "volume"),  # base units over 24h
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "ts": int(time.time()),
    }

def coinbase_links(pair):
    base = pair.split("-")[0].lower()
    return {
        "advanced": f"https://www.coinbase.com/advanced-trade/spot/{pair}",
        "price":    f"https://www.coinbase.com/price/{base}",
    }

# Candles: Coinbase returns up to 300 candles per request.
def fetch_hourly_candles(pair, days=30, gran=3600):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    total_hours = int((now - start).total_seconds() // gran)
    candles = []
    cursor_end = now
    remaining = total_hours
    while remaining > 0:
        take = min(300, remaining)
        cursor_start = cursor_end - timedelta(seconds=gran * take)
        params = {"start": cursor_start.isoformat(), "end": cursor_end.isoformat(), "granularity": gran}
        data = cb_get(f"/products/{pair}/candles", **params)
        # Each item: [time, low, high, open, close, volume]
        candles.extend(data)
        cursor_end = cursor_start
        remaining -= take
    candles.sort(key=lambda c: c[0])  # ascending by time
    return candles

# =========================
# Quality + Probability
# =========================
def is_quality(snap):
    """
    Keep assets that actually transact:
      - not in SYMBOL_BLACKLIST
      - if SYMBOL_WHITELIST is set, only keep those symbols
      - enforce 24h dollar volume and max bid/ask spread
    """
    base = snap["pair"].split("-")[0].upper()

    if base in SYMBOL_BLACKLIST:
        return False
    if SYMBOL_WHITELIST and base not in SYMBOL_WHITELIST:
        return False

    dollar_vol = float(snap["price"]) * float(snap["volume"])
    if dollar_vol < MIN_24H_DOLLAR_VOL:
        return False

    if snap.get("spread_pct", 1.0) > MAX_SPREAD_PCT:
        return False

    return True

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def probability_score(snap):
    """
    Proxy Prob( +70% in <30d ). Replace later with calibrated model.
    Inputs (0..1):
      - Momentum m: (price-open)/open capped [-0.2,+0.2]
      - Expansion e: (high-low)/open capped [0,0.25]
      - Volume v: log10(volume) scaled [0,1]
    Score = 0.50*m + 0.35*e + 0.15*v  ->  prob in [0.01, 0.99]
    """
    p, o, h, l, v = snap["price"], snap["open"], snap["high"], snap["low"], snap["volume"]
    if o <= 0 or p <= 0:
        return 0.05

    mom_raw = clamp((p - o) / o, -0.20, 0.20)      # -20%..+20%
    m = (mom_raw + 0.20) / 0.40                    # 0..1

    exp_raw = clamp((h - l) / max(o, 1e-9), 0.0, 0.25)
    e = exp_raw / 0.25

    v_scaled = clamp((math.log10(max(v, 1e-6)) - 0) / 8, 0.0, 1.0)
    score = 0.50*m + 0.35*e + 0.15*v_scaled

    prob = clamp(0.05 + 0.90*score, 0.01, 0.99)
    return prob

# =========================
# Seasonality (hour-of-day)
# =========================
def hourly_seasonality(pair, days=30, gran=3600):
    """
    Computes hour-of-day profile over N days (UTC).
    For each day, normalize each hour's close by that day's mean/std (z-score),
    then average z per hour across days.
    Returns: dict with mean_z_by_hour[0..23], buy_hours, sell_hours.
    """
    try:

