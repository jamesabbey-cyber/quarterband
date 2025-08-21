# app.py
import os
import math
import time
import requests
from functools import wraps
from flask import Flask, request, Response, render_template_string, jsonify

app = Flask(__name__)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "QuarterBand/probability"})

# --------- Config ---------
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = os.getenv("QB_PASS", "password")
COINBASE_EXCHANGE_API = "https://api.exchange.coinbase.com"

# Price window (defaults 0.10–0.25, can override via env)
PRICE_MIN = float(os.getenv("PRICE_MIN", "0.10"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "0.25"))

# --------- Auth ---------
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

# --------- Coinbase helpers ---------
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
    """Return price + 24h stats for a product id like 'XYZ-USD'."""
    t = cb_get(f"/products/{pair}/ticker")
    s = cb_get(f"/products/{pair}/stats")
    def f(k, d=0.0):
        try: return float(s.get(k, d))
        except Exception: return d
    price = float(t.get("price") or t.get("last") or 0.0)
    return {
        "pair": pair,
        "price": price,
        "open": f("open"),
        "high": f("high"),
        "low":  f("low"),
        "volume": f("volume"),
        "ts": int(time.time()),
    }

def coinbase_trade_url(pair):
    return f"https://www.coinbase.com/advanced-trade/spot/{pair}"

# --------- Probability (proxy) ---------
def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def probability_score(snap):
    """
    Proxy Prob( +70% in <30d ).
    Replace with your calibrated model later. Inputs (0..1 each):
      - Momentum m: (price - open)/open capped [-0.2,+0.2] → 0..1
      - Expansion e: (high-low)/open capped [0,0.25] → 0..1
      - Volume v: log10(volume) scaled to [0,1] (heuristic)
    Score = 0.5*m + 0.35*e + 0.15*v → map to 0..1
    """
    p, o, h, l, v = snap["price"], snap["open"], snap["high"], snap["low"], snap["volume"]
    if o <= 0 or p <= 0:
        return 0.05

    mom_raw = clamp((p - o) / o, -0.20, 0.20)   # -20%..+20%
    m = (mom_raw + 0.20) / 0.40                 # → 0..1

    exp_raw = clamp((h - l) / max(o, 1e-9), 0.0, 0.25)  # 0..25%
    e = exp_raw / 0.25

    v_scaled = clamp((math.log10(max(v, 1e-6)) - 0) / 8, 0.0, 1.0)
    score = 0.50*m + 0.35*e + 0.15*v_scaled

    prob = clamp(0.05 + 0.90*score, 0.01, 0.99)
    return prob

# --------- UI ---------
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
    <div class="muted">Coinbase USD markets • Price range: ${{ "%.2f"|format(price_min) }}–${{ "%.2f"|format(price_max) }} • Ranked by proxy probability of +70% in &lt;30d</div>

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
          <tr><th>Pair</th><th>Price</th><th>Open</th><th>High</th><th>Low</th><th>Volume</th><th>Prob +70%/&lt;30d</th></tr>
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
              <td>{{ "%.1f"|format(r.prob*100) }}%</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      <div class="muted" style="margin-top:6px;">* Probabilities are an explanatory proxy for now; not financial advice.</div>
    </div>
  </div>
</body>
</html>
"""

# --------- Routes ---------
@app.route("/")
@requires_auth
def index():
    products = list_usd_products()

    snaps = []
    for p in products:
        try:
            s = fetch_snapshot(p["id"])
            # Price filter window: only 0.10–0.25 (defaults; configurable via env)
            if PRICE_MIN <= s["price"] <= PRICE_MAX:
                snaps.append(s)
        except Exception:
            continue

    # Score + enrich
    enriched = []
    for s in snaps:
        prob = probability_score(s)
        enriched.append({**s,
            "prob": prob,
            "trade_url": coinbase_trade_url(s["pair"])
        })

    # Rank by highest probability
    ranked = sorted(enriched, key=lambda x: x["prob"], reverse=True)

    return render_template_string(
        INDEX_TEMPLATE,
        ranked=ranked,
        price_min=PRICE_MIN,
        price_max=PRICE_MAX
    )

@app.route("/healthz")
def healthz():
    return jsonify(status="ok", ts=int(time.time()))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
