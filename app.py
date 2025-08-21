# app.py
import os
import math
import time
import requests
from functools import wraps
from flask import Flask, request, Response, render_template_string, jsonify

app = Flask(__name__)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "QuarterBand/quality-prob"})

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

# ---- Quality filter knobs ----
# Liquidity (approx) = last price * 24h base volume
MIN_24H_DOLLAR_VOL = float(os.getenv("MIN_24H_DOLLAR_VOL", "20000000"))  # $20M default
# Bid/ask spread (% of mid); lower = tighter market
MAX_SPREAD_PCT     = float(os.getenv("MAX_SPREAD_PCT", "0.008"))         # 0.8% default
# Optional lists (BASE symbols, no "-USD")
SYMBOL_WHITELIST = {x.strip().upper() for x in os.getenv("SYMBOL_WHITELIST", "").split(",") if x.strip()}
SYMBOL_BLACKLIST = {x.strip().upper() for x in os.getenv(
    "SYMBOL_BLACKLIST",
    "USLESS,COOKIE,PNUT,PEPE,SHIB,FLOKI,WIF,BONK"
).split(",") if x.strip()}

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
    r = SESSION.get(url, params=params, timeout=10)
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

def coinbase_trade_url(pair):
    return f"https://www.coinbase.com/advanced-trade/spot/{pair}"

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
# UI Template
# =========================
INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>QuarterBand 70/30</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="60" />
  <style>
    :root { color-scheme: dark; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Inter, sans-serif; background:#0b1220; color:#e6edf3; }
    .wrap { max-width: 1100px; margin: 32px auto; padding: 0 16px; }
    h1 { margin:0 0 6px; font-size: 28px; }
    .muted { color:#9aa4b2; }
    .grid { display:grid; grid-template-columns: repeat(auto-fill,minmax(240px,1fr)); gap:16px; margin-top:20px; }
    .tile { display:block; text-decoration:none; background:#111a2b; border:1px solid #1c2940; border-radius:14px; padding:16px; transition: background .15s, transform .06s; }
    .tile:hover { background:#15233a; transform: translateY(-1px); }
    .pair { font-weight:700; font-size:18px; color:#e6edf3; }
    .price { margin-top:6px; font-size:22px; color:#cbd5e1; }
    .prob { margin-top:8px; font-size:13px; color:#9aa4b2; }
    .badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; background:#1f2e4a; color:#e6edf3; margin-left:8px; }
    .section { margin-top: 28px; }
    table { width:100%; border-collapse:collapse; }
    th,td { padding:10px 8px; border-bottom:1px solid #1c2940; font-size:14px; }
    th { text-align:left; color:#9aa4b2; font-weight:600; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>QuarterBand 70/30 <span class="badge">Auto-refresh 60s</span></h1>
    <div class="muted">
      Coinbase USD markets • Effective price window:
      ${{ "%.2f"|format(eff_min) }}–${{ "%.2f"|format(eff_max) }}
      • Ranked by proxy probability of +70% in &lt;30d
    </div>

    <div class="grid">
      {% for r in ranked %}
        <a class="tile" href="{{ r.trade_url }}" target="_blank" rel="noopener noreferrer" aria-label="Open {{ r.pair }} on Coinbase">
          <div class="pair">{{ r.pair }}</div>
          <div class="price">${{ "%.4f"|format(r.price) }}</div>
          <div class="prob">Prob( +70% / &lt;30d ): <strong>{{ "%.1f"|format(r.prob*100) }}%</strong></div>
        </a>
      {% endfor %}
    </div>

    <div class="section">
      <h2>Details</h2>
      <table>
        <thead>
          <tr><th>Pair</th><th>Price</th><th>Open</th><th>High</th><th>Low</th><th>Volume</th><th>Spread</th><th>Prob +70%/&lt;30d</th></tr>
        </thead>
        <tbody>
          {% for r in ranked %}
            <tr>
              <td><a href="{{ r.trade_url }}" target="_blank" rel="noopener noreferrer">{{ r.pair }}</a></td>
              <td>${{ "%.4f"|format(r.price) }}</td>
              <td>${{ "%.4f"|format(r.open) }}</td>
              <td>${{ "%.4f"|format(r.high) }}</td>
              <td>${{ "%.4f"|format(r.low) }}</td>
              <td>{{ "%.0f"|format(r.volume) }}</td>
              <td>{{ "%.2f"|format(r.spread_pct*100) }}%</td>
              <td>{{ "%.1f"|format(r.prob*100) }}%</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      <div class="muted" style="margin-top:6px;">* Probabilities are an explanatory proxy; informational only, not financial advice.</div>
    </div>
  </div>
</body>
</html>
"""

# =========================
# Routes
# =========================
@app.route("/")
@requires_auth
def index():
    # 1) Fetch all USD snapshots once
    products = list_usd_products()
    all_snaps = []
    for p in products:
        try:
            all_snaps.append(fetch_snapshot(p["id"]))
        except Exception:
            continue

    # 2) Drop meme/illiquid/wide-spread names first
    all_snaps = [s for s in all_snaps if is_quality(s)]

    # 3) Apply price window; widen upper bound until we hit MIN_COUNT or cap
    eff_min = PRICE_MIN
    eff_max = PRICE_MAX

    def in_window(s): return eff_min <= s["price"] <= eff_max
    candidates = [s for s in all_snaps if in_window(s)]

    while len(candidates) < MIN_COUNT and eff_max < MAX_PRICE_CAP:
        eff_max = min(eff_max + EXPAND_STEP, MAX_PRICE_CAP)
        candidates = [s for s in all_snaps if eff_min <= s["price"] <= eff_max]

    # 4) Score + enrich
    enriched = []
    for s in candidates:
        prob = probability_score(s)
        enriched.append({**s,
            "prob": prob,
            "trade_url": coinbase_trade_url(s["pair"])
        })

    # 5) Rank & keep TOP_K
    ranked = sorted(enriched, key=lambda x: x["prob"], reverse=True)[:TOP_K]

    return render_template_string(
        INDEX_TEMPLATE,
        ranked=ranked,
        eff_min=eff_min,
        eff_max=eff_max,
    )

@app.route("/healthz")
def healthz():
    return jsonify(status="ok", ts=int(time.time()))

# Alias for monitors that expect this path
@app.route("/api/health")
def api_health():
    return jsonify(status="ok", ts=int(time.time()))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
