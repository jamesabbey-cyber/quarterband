# app.py
import os
import math
import time
import requests
from functools import wraps
from flask import Flask, request, Response, render_template_string, jsonify

app = Flask(__name__)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "QuarterBand/1.0 (+https://quarterband)"})

COINBASE_EXCHANGE_API = "https://api.exchange.coinbase.com"  # public, no key required

###############################################################################
# Auth
###############################################################################
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = os.getenv("QB_PASS", "password")

def check_auth(username, password):
    return username == QB_USER and password == QB_PASS

def authenticate():
    return Response(
        "Authentication required", 401,
        {"WWW-Authenticate": 'Basic realm="QuarterBand"'}
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

###############################################################################
# Data fetch
###############################################################################
def coinbase_get(path, **params):
    url = f"{COINBASE_EXCHANGE_API}{path}"
    r = SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def list_usd_products():
    """
    Returns product dicts like {'id':'BTC-USD', 'base_currency':'BTC', ...}
    Filters: quote_currency=USD, status='online', trading disabled False
    """
    products = coinbase_get("/products")
    out = []
    for p in products:
        if (
            p.get("quote_currency") == "USD" and
            p.get("status") == "online" and
            not p.get("trading_disabled", False)
        ):
            out.append({"id": p["id"], "base": p["base_currency"]})
    return out

def fetch_snapshot(product_id):
    """
    Grabs last price and 24h stats for a product.
    /ticker -> last price
    /stats  -> open, high, low, volume
    """
    ticker = coinbase_get(f"/products/{product_id}/ticker")
    stats = coinbase_get(f"/products/{product_id}/stats")
    price = float(ticker.get("price") or ticker.get("last") or 0.0)

    # stats fields come as strings
    def f(k, default=0.0):
        try:
            return float(stats.get(k, default))
        except Exception:
            return default

    open_px = f("open")
    high = f("high")
    low = f("low")
    volume = f("volume")
    return {
        "pair": product_id,
        "price": price,
        "open": open_px,
        "high": high,
        "low": low,
        "volume": volume,
        "ts": int(time.time()),
    }

###############################################################################
# Scoring (proxy for probability of +70% in 30 days)
###############################################################################
def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def probability_score(snap):
    """
    Simple, explainable proxy while you build the real model.
    Components (all 0..1 then weighted):
      - Momentum (m): (price - open)/open (capped to [-0.2, +0.2]) -> rescale to 0..1
      - Intraday expansion (e): (high-low)/open (cap 0..0.25)  -> 0..1
      - Volume impulse (v): log(volume) vs. heuristic range -> 0..1
    Score = 0.5*m + 0.35*e + 0.15*v, then mapped to a pseudo probability 0..1
    """
    price, open_px, high, low, vol = (
        snap["price"], snap["open"], snap["high"], snap["low"], snap["volume"]
    )
    if open_px <= 0 or price <= 0:
        return 0.05

    # Momentum: favor positive drift since open
    mom_raw = clamp((price - open_px) / open_px, -0.20, 0.20)  # -20%..+20%
    m = (mom_raw + 0.20) / 0.40  # -> 0..1

    # Expansion: wider range suggests potential breakout regime
    exp_raw = clamp((high - low) / max(open_px, 1e-9), 0.0, 0.25)  # 0..25%
    e = exp_raw / 0.25

    # Volume impulse: log-scale normalization (heuristic)
    v = clamp((math.log10(max(vol, 1e-6)) - 0) / 8, 0.0, 1.0)

    score = 0.50 * m + 0.35 * e + 0.15 * v

    # Map score to pseudo-probability of +70%/30d.
    # NOTE: Replace with your calibrated model later.
    prob = clamp(0.05 + 0.90 * score, 0.01, 0.99)
    return prob

###############################################################################
# Entry / Exit discipline
###############################################################################
def entry_exit_bands(snap):
    """
    Naive, rule-based bands to start (replace with backtested model):
      - Entry: price within 3% of (open + 20% of (high-low))  => early strength
      - Stop:  -8% from entry_ref
      - TP1:   +20%, TP2: +70% (target condition)
    """
    open_px, high, low, price = snap["open"], snap["high"], snap["low"], snap["price"]
    if open_px <= 0:
        return {"entry_band": None, "stop": None, "tp1": None, "tp2": None}

    rng = max(high - low, 0)
    entry_ref = open_px + 0.20 * rng
    band_lo = entry_ref * 0.97
    band_hi = entry_ref * 1.03
    stop = entry_ref * 0.92        # -8%
    tp1 = entry_ref * 1.20         # +20%
    tp2 = entry_ref * 1.70         # +70%

    should_enter_now = band_lo <= price <= band_hi

    return {
        "entry_ref": entry_ref,
        "entry_band": (band_lo, band_hi),
        "enter_now": should_enter_now,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
    }

###############################################################################
# Coinbase links
###############################################################################
def coinbase_trade_url(pair: str) -> str:
    return f"https://www.coinbase.com/advanced-trade/spot/{pair}"

###############################################################################
# Routes
###############################################################################
INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>QuarterBand 70/30</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <!-- Auto refresh -->
  <meta http-equiv="refresh" content="60" />
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Inter, sans-serif; background: #0b1220; color: #e6edf3; }
    .wrap { max-width: 1100px; margin: 32px auto; padding: 0 16px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    .muted { color: #9aa4b2; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(240px,1fr)); gap: 16px; margin-top: 20px; }
    .tile { display: block; background: #111a2b; border-radius: 14px; padding: 16px; text-decoration: none; border: 1px solid #1c2940; transition: background .15s, transform .06s; }
    .tile:hover { background: #15233a; transform: translateY(-1px); }
    .pair { font-weight: 700; font-size: 18px; color: #e6edf3; }
    .price { margin-top: 6px; font-size: 22px; color: #cbd5e1; }
    .prob { margin-top: 10px; font-size: 13px; color: #9aa4b2; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background:#1f2e4a; color:#e6edf3; margin-left:8px; }
    .section { margin-top: 30px; }
    table { width:
