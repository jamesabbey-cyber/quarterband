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
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px 8px; border-bottom: 1px solid #1c2940; font-size: 14px; }
    th { text-align: left; color: #9aa4b2; font-weight: 600; }
    .ok { color: #34d399; }
    .warn { color: #f59e0b; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>QuarterBand 70/30 <span class="badge">Auto-refresh 60s</span></h1>
    <div class="muted">Basic-Auth protected • Coinbase USD market scan • Price ≥ $0.10 • Ranked by proxy probability of +70% in 30d</div>

    <div class="grid">
      {% for r in ranked %}
        <a class="tile" href="{{ r.trade_url }}" target="_blank" rel="noopener noreferrer" aria-label="Open {{ r.pair }} on Coinbase">
          <div class="pair">{{ r.pair }}</div>
          <div class="price">${{ "%.4f"|format(r.price) }}</div>
          <div class="prob">
            Prob( +70% / 30d ): <strong>{{ "%.1f"|format(r.prob*100) }}%</strong>
            {% if r.enter_now %}
              <span class="badge">Entry zone</span>
            {% endif %}
          </div>
        </a>
      {% endfor %}
    </div>

    <div class="section">
      <h2>Opportunity details</h2>
      <table>
        <thead>
          <tr>
            <th>Pair</th><th>Price</th><th>Open</th><th>High</th><th>Low</th>
            <th>Prob +70%/30d</th><th>Entry Band</th><th>Stop</th><th>TP1</th><th>TP2</th>
          </tr>
        </thead>
        <tbody>
          {% for r in ranked %}
            <tr>
              <td><a href="{{ r.trade_url }}" target="_blank" rel="noopener noreferrer">{{ r.pair }}</a></td>
              <td>${{ "%.4f"|format(r.price) }}</td>
              <td>${{ "%.4f"|format(r.open) }}</td>
              <td>${{ "%.4f"|format(r.high) }}</td>
              <td>${{ "%.4f"|format(r.low) }}</td>
              <td>{{ "%.1f"|format(r.prob*100) }}%</td>
              <td>
                {% if r.entry_band %}
                  ${{ "%.4f"|format(r.entry_band[0]) }}–${{ "%.4f"|format(r.entry_band[1]) }}
                  {% if r.enter_now %}<span class="ok">✓</span>{% endif %}
                {% else %}-{% endif %}
              </td>
              <td>${{ "%.4f"|format(r.stop) }}</td>
              <td>${{ "%.4f"|format(r.tp1) }}</td>
              <td>${{ "%.4f"|format(r.tp2) }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      <div class="muted" style="margin-top:8px;">
        * Probabilities are model proxies for demonstration only; not financial advice. Replace with your calibrated model.
      </div>
    </div>
  </div>
</body>
</html>
"""

@app.route("/")
@requires_auth
def index():
    # 1) Discover USD markets on Coinbase
    usd_products = list_usd_products()

    # 2) Fetch snapshots and filter price >= $0.10
    snapshots = []
    for p in usd_products:
        try:
            s = fetch_snapshot(p["id"])
            if s["price"] >= 0.10:
                snapshots.append(s)
        except Exception:
            continue

    # 3) Score & enrich
    enriched = []
    for s in snapshots:
        prob = probability_score(s)
        bands = entry_exit_bands(s)
        enriched.append({
            **s,
            "prob": prob,
            "entry_ref": bands.get("entry_ref"),
            "entry_band": bands.get("entry_band"),
            "enter_now": bands.get("enter_now"),
            "stop": bands.get("stop"),
            "tp1": bands.get("tp1"),
            "tp2": bands.get("tp2"),
            "trade_url": coinbase_trade_url(s["pair"]),
        })

    # 4) Rank: highest probability first, top 50 to keep it snappy
    ranked = sorted(enriched, key=lambda x: x["prob"], reverse=True)[:50]

    return render_template_string(INDEX_TEMPLATE, ranked=ranked)

@app.route("/healthz")
def healthz():
    return jsonify(status="ok", ts=int(time.time()))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
