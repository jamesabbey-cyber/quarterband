# app.py
# QuarterBand 70/30 – FastAPI (single file)
# Private via Basic Auth (APP_USER/APP_PASS envs). With 7-day momentum from candles.

import os
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, DictLoader, select_autoescape

# -------- Config (env-overridable) --------
PRICE_MIN = float(os.getenv("PRICE_MIN", "0.10"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "0.25"))
MIN_24H_DOLLAR_VOL = float(os.getenv("MIN_24H_DOLLAR_VOL", "10000000"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.35"))
TOP_K = int(os.getenv("TOP_K", "13"))
# refresh cadence (seconds); can override in Render env as REFRESH_SECONDS=30
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))

# in-memory cache the background job will keep current
CACHE: Dict[str, Any] = {
    "picks": [],
    "last_refresh": None,   # ISO timestamp
}


APP_USER = os.getenv("APP_USER", "admin")       # set on host
APP_PASS = os.getenv("APP_PASS", "change-me")   # set on host

CB_BASE = "https://api.exchange.coinbase.com"

app = FastAPI(title="QuarterBand 70/30", version="0.4.0")
security = HTTPBasic()

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != APP_USER or credentials.password != APP_PASS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# -------- Coinbase helpers --------
async def cb_get(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> Optional[dict | list]:
    r = await client.get(f"{CB_BASE}{path}", params=params or {})
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None

async def get_products(client: httpx.AsyncClient) -> List[dict]:
    data = await cb_get(client, "/products")
    if not isinstance(data, list):
        return []
    return [p for p in data if p.get("quote_currency") == "USD" and not p.get("trading_disabled", False)]

async def get_ticker(client: httpx.AsyncClient, product_id: str) -> Optional[dict]:
    return await cb_get(client, f"/products/{product_id}/ticker")

async def get_stats24h(client: httpx.AsyncClient, product_id: str) -> Optional[dict]:
    return await cb_get(client, f"/products/{product_id}/stats")

async def get_orderbook_top(client: httpx.AsyncClient, product_id: str) -> Optional[dict]:
    return await cb_get(client, f"/products/{product_id}/book", params={"level": 1})

async def get_daily_candles(client: httpx.AsyncClient, product_id: str, days: int = 8) -> Optional[List[List[float]]]:
    """
    Coinbase 'candles' returns arrays: [time, low, high, open, close, volume]
    We'll request a tight window: last (days) days.
    """
    end = datetime.utcnow().replace(microsecond=0)
    start = end - timedelta(days=days + 1)
    params = {"start": start.isoformat(), "end": end.isoformat(), "granularity": 86400}
    data = await cb_get(client, f"/products/{product_id}/candles", params=params)
    if not isinstance(data, list):
        return None
    try:
        data.sort(key=lambda c: c[0])  # sort by time ascending
    except Exception:
        pass
    return data

def compute_spread_pct(book: dict) -> Optional[float]:
    try:
        best_bid = float(book["bids"][0][0])
        best_ask = float(book["asks"][0][0])
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid * 100.0
    except Exception:
        return None

def dollar_volume_24h(stats24h: dict) -> Optional[float]:
    try:
        base_vol = float(stats24h["volume"])
        last = float(stats24h["last"])
        return base_vol * last
    except Exception:
        return None

# -------- Scoring --------
def momentum_score(pct_change_24h: Optional[float], pct_change_7d: Optional[float], spread_pct: Optional[float]) -> float:
    score = 0.0
    if pct_change_24h is not None:
        if 5 <= pct_change_24h <= 20: score += 0.4
        elif 0 <= pct_change_24h < 5: score += 0.2
        elif -2 <= pct_change_24h < 0: score += 0.05
    if pct_change_7d is not None:
        if pct_change_7d >= 8: score += 0.3
        elif pct_change_7d >= 3: score += 0.15
    if spread_pct is not None:
        if spread_pct <= 0.25: score += 0.3
        elif spread_pct <= 0.35: score += 0.15
    return min(score, 1.0)

def probability_from_scores(momentum: float, social: float = 0.0, catalyst: float = 0.0) -> float:
    p = 0.6 * momentum + 0.2 * social + 0.2 * catalyst
    return max(0.0, min(p, 1.0)) * 100.0

def risk_drawdown_proxies(true_range_24h_pct: Optional[float]) -> tuple[float, float]:
    if true_range_24h_pct is None:
        return (18.0, 32.0)
    p50 = max(8.0, 1.2 * true_range_24h_pct)
    p90 = max(15.0, 2.1 * true_range_24h_pct)
    return (p50, p90)

# -------- Fetch + Rank --------
async def fetch_one(client: httpx.AsyncClient, product: dict) -> Optional[Dict[str, Any]]:
    pid = product.get("id")
    base_name = product.get("base_name") or product.get("base_currency")

    ticker_task  = asyncio.create_task(get_ticker(client, pid))
    stats_task   = asyncio.create_task(get_stats24h(client, pid))
    book_task    = asyncio.create_task(get_orderbook_top(client, pid))
    candles_task = asyncio.create_task(get_daily_candles(client, pid, days=8))

    ticker, stats, book, candles = await asyncio.gather(ticker_task, stats_task, book_task, candles_task)
    if not ticker or not stats or not book:
        return None

    try:
        last = float(ticker["price"])
        open_ = float(stats["open"])
        pct_change_24h = ((last - open_) / open_) * 100.0 if open_ > 0 else None
    except Exception:
        return None

    pct_change_7d = None
    try:
        if isinstance(candles, list) and len(candles) >= 7:
            close_now = float(candles[-1][4])
            close_7ago = float(candles[-7][4])
            if close_7ago > 0:
                pct_change_7d = (close_now - close_7ago) / close_7ago * 100.0
    except Exception:
        pct_change_7d = None

    spread_pct = compute_spread_pct(book)
    dv = dollar_volume_24h(stats)

    true_range_24h_pct = None
    try:
        high = float(stats["high"]); low = float(stats["low"])
        if last > 0 and high > 0 and low > 0:
            true_range_24h_pct = (high - low) / last * 100.0
    except Exception:
        pass

    in_band = (PRICE_MIN <= last <= PRICE_MAX)
    liquid  = (dv is not None and dv >= MIN_24H_DOLLAR_VOL)
    tight   = (spread_pct is not None and spread_pct <= MAX_SPREAD_PCT)
    eligible = in_band and liquid and tight

    if not eligible:
        return {
            "product_id": pid,
            "symbol": base_name,
            "name": base_name,
            "price": last,
            "eligible": False,
            "reason": {"in_band": in_band, "liquid": liquid, "tight": tight},
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    mom = momentum_score(pct_change_24h, pct_change_7d, spread_pct)
    prob = probability_from_scores(momentum=mom, social=0.0, catalyst=0.0)
    p50_dd, p90_dd = risk_drawdown_proxies(true_range_24h_pct)

    return {
        "product_id": pid,
        "symbol": base_name,
        "name": base_name,
        "price": last,
        "pct_change_24h": pct_change_24h,
        "pct_change_7d": pct_change_7d,
        "spread_pct": spread_pct,
        "dollar_volume_24h": dv,
        "in_price_band": in_band,
        "eligible": True,
        "prob_70_30d": prob,
        "momentum_score": mom,
        "risk_p50_drawdown_pct": p50_dd,
        "risk_p90_drawdown_pct": p90_dd,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

async def get_ranked_assets() -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=25) as client:
        products = await get_products(client)
        sem = asyncio.Semaphore(12)
        async def guarded(p):
            async with sem:
                return await fetch_one(client, p)
        results = await asyncio.gather(*[guarded(p) for p in products], return_exceptions=False)

    eligible = [r for r in results if r and r.get("eligible")]
    eligible.sort(key=lambda x: (x["prob_70_30d"], x.get("dollar_volume_24h") or 0.0), reverse=True)
    return eligible[:TOP_K]

# -------- Routes --------
HTML_TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><title>QuarterBand 70/30</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="{{ refresh_seconds }}">
<meta http-equiv="refresh" content="{{ refresh_seconds }}">

<!-- Chart.js for graphs -->
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
:root{--bg:#0b0f14;--card:#111826;--text:#e6edf3;--muted:#97a3ad;--accent:#5b9cff;--border:#1f2937}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Segoe UI,Arial;background:var(--bg);color:var(--text)}
header{padding:24px;border-bottom:1px solid var(--border)}h1{margin:0 0 8px 0;font-size:24px}p{margin:0;color:var(--muted)}
main{padding:24px}.empty{padding:24px;border:1px dashed var(--border);border-radius:12px;color:var(--muted)}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px}
.card-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
h2{margin:0;font-size:20px}.badge{background:rgba(91,156,255,.15);color:#b3d3ff;padding:4px 8px;border-radius:999px;font-size:12px;border:1px solid rgba(91,156,255,.35)}
.row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--border)}.row:last-of-type{border-bottom:none}
.row.small{font-size:12px;color:var(--muted)}footer{margin-top:8px;color:var(--muted);font-size:12px}
.footnote{padding:16px 24px;border-top:1px solid var(--border);color:var(--muted)}
</style></head><body>
<header><h1>QuarterBand 70/30</h1>
<p>Coinbase tokens in the ${{ price_min }}–${{ price_max }} band with highest estimated chance to hit +70% in 30 days.</p>
</header><main>
{% if picks|length == 0 %}
<div class="empty">No eligible tokens right now. Check back soon.</div>
{% else %}
<div class="grid">
{% for p in picks %}
<article class="card"><div class="card-title"><h2>{{ p.symbol }}</h2>
<span class="badge">P(≥+70%/30d): {{ "%.1f"|format(p.prob_70_30d) }}%</span></div>
<div class="row"><div><strong>Price</strong></div><div>${{ "%.4f"|format(p.price) }}</div></div>
<div class="row"><div><strong>24h Change</strong></div><div>{% if p.pct_change_24h is not none %}{{ "%.2f"|format(p.pct_change_24h) }}%{% else %}—{% endif %}</div></div>
<div class="row"><div><strong>7d Change</strong></div><div>{% if p.pct_change_7d is not none %}{{ "%.2f"|format(p.pct_change_7d) }}%{% else %}—{% endif %}</div></div>
<div class="row"><div><strong>Spread</strong></div><div>{% if p.spread_pct is not none %}{{ "%.3f"|format(p.spread_pct) }}%{% else %}—{% endif %}</div></div>
<div class="row"><div><strong>24h Volume</strong></div><div>{% if p.dollar_volume_24h %}${{ "{:,.0f}".format(p.dollar_volume_24h) }}{% else %}—{% endif %}</div></div>
<div class="row"><div><strong>Risk (p50 / p90 DD)</strong></div><div>{{ "%.0f"|format(p.risk_p50_drawdown_pct) }}% / {{ "%.0f"|format(p.risk_p90_drawdown_pct) }}%</div></div>
<div class="row small"><div><strong>Momentum Score</strong></div><div>{{ "%.2f"|format(p.momentum_score) }}</div></div>
<footer><small>As of {{ p.as_of }}</small></footer></article>
{% endfor %}
</div>
{% endif %}
</main><footer class="footnote"><p><strong>Disclaimer:</strong> Informational only, not financial advice. Crypto is volatile; you can lose capital.</p></footer>
</body></html>
"""
env = Environment(loader=DictLoader({"index.html": HTML_TEMPLATE}),
                  autoescape=select_autoescape(["html"]))
# ==========================================================
# 🔄 Auto-refresh cache every 30 seconds when the app starts
# ==========================================================

REFRESH_SECONDS = 30
last_refresh = None

@app.on_event("startup")
async def start_refresh():
    asyncio.create_task(refresh_loop())

async def refresh_loop():
    global last_refresh
    while True:
        try:
            picks = await get_ranked_assets()
            CACHE["picks"] = picks
            last_refresh = datetime.utcnow()
            print(f"✅ Cache updated at {last_refresh}")
        except Exception as e:
            print(f"⚠️ Error refreshing cache: {e}")
        await asyncio.sleep(REFRESH_SECONDS)

# -------- Candles API for charts --------
@app.get("/api/candles", dependencies=[Depends(check_auth)])
async def api_candles(product_id: str, days: int = 30, granularity: int = 86400):
    """
    Returns daily or hourly candles for a product_id.
    granularity=86400 => daily; 3600 => hourly, etc.
    Response: {"t": [iso times...], "c": [close prices...]}
    """
    end = datetime.utcnow().replace(microsecond=0)
    start = end - timedelta(days=days + 1)
    params = {"start": start.isoformat(), "end": end.isoformat(), "granularity": granularity}
    async with httpx.AsyncClient(timeout=25) as client:
        data = await cb_get(client, f"/products/{product_id}/candles", params=params)
    if not isinstance(data, list):
        return JSONResponse({"t": [], "c": []})
    try:
        data.sort(key=lambda c: c[0])  # ascending by time
    except Exception:
        pass
    ts = [datetime.utcfromtimestamp(int(row[0])).isoformat() + "Z" for row in data]
    closes = [float(row[4]) for row in data]
    return JSONResponse({"t": ts, "c": closes})
# ==========================================================
# ⬇️ Routes
# ==========================================================

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def home(_: Request):
    tmpl = env.get_template("index.html")
    return tmpl.render(
        picks=CACHE.get("picks", []),
        price_min=PRICE_MIN,
        price_max=PRICE_MAX,
        refresh_seconds=REFRESH_SECONDS,  # <-- this is the new line
    )

@app.get("/api/top-picks", dependencies=[Depends(check_auth)])
async def api_top_picks():
    return JSONResponse({
        "last_refresh": last_refresh,
        "picks": CACHE.get("picks", [])
    })

@app.get("/api/health")
async def health():
    return {"status": "ok", "last_refresh": last_refresh}



