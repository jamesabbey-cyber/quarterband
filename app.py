# app.py — QuarterBand minimal tracker with charts + basic auth

import os
import asyncio
import secrets
from datetime import datetime, timedelta

import aiohttp
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.requests import Request

# -------- Config --------
COINBASE_API = "https://api.exchange.coinbase.com"
# Pairs to track (you can expand this list later)
PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "AVAX-USD", "DOGE-USD"]

# refresh page & price-poller cadence
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))

# Basic auth (set APP_USER / APP_PASS in Render)
APP_USER = os.getenv("APP_USER", "admin")
APP_PASS = os.getenv("APP_PASS", "password")

security = HTTPBasic()

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not (
        secrets.compare_digest(credentials.username, APP_USER)
        and secrets.compare_digest(credentials.password, APP_PASS)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

app = FastAPI(title="QuarterBand", version="0.1.0")

# in-memory price cache
prices = {}  # { "BTC-USD": 64321.0, ... }

async def fetch_price(session: aiohttp.ClientSession, pair: str):
    url = f"{COINBASE_API}/products/{pair}/ticker"
    try:
        async with session.get(url, timeout=15) as resp:
            data = await resp.json()
            return pair, float(data["price"])
    except Exception:
        return pair, None

async def price_loop():
    global prices
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                tasks = [fetch_price(session, p) for p in PAIRS]
                results = await asyncio.gather(*tasks, return_exceptions=False)
                prices = {p: v for p, v in results if v is not None}
        except Exception as e:
            print("price_loop error:", e)
        await asyncio.sleep(REFRESH_SECONDS)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(price_loop())

# ---------------- Routes ----------------

HTML = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><title>QuarterBand 70/30</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="{{ refresh_seconds }}">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  body{margin:0;background:#0b0f14;color:#e6edf3;font-family:system-ui,Arial}
  header{padding:24px;border-bottom:1px solid #1f2937}
  h1{margin:0 0 6px 0}
  #cards,#charts{display:flex;flex-wrap:wrap;gap:12px;justify-content:center;padding:16px}
  .card{background:#111826;border:1px solid #1f2937;border-radius:12px;padding:14px;min-width:160px;text-align:center}
  canvas{background:#111826;border:1px solid #1f2937;border-radius:12px;padding:12px;margin:6px}
</style>
</head><body>
<header>
  <h1>QuarterBand 70/30</h1>
  <div style="opacity:.7">Auto-refreshes every {{ refresh_seconds }}s • Basic-Auth protected</div>
</header>

<section id="cards"></section>

<h2 style="text-align:center;margin:0;padding:8px 0 0 0;">Performance charts</h2>
<section id="charts"></section>

<script>
async function load() {
  const picks = await (await fetch('/api/top-picks', {cache:'no-store'})).json();

  // price cards
  const cards = document.getElementById('cards');
  cards.innerHTML = '';
  picks.forEach(p => {
    const d = document.createElement('div');
    d.className = 'card';
    d.innerHTML = `<div style="font-weight:600">${p.pair}</div><div>$${p.price.toFixed(4)}</div>`;
    cards.appendChild(d);
  });

  // charts (first 3)
  const charts = document.getElementById('charts');
  charts.innerHTML = '';
  for (const p of picks.slice(0,3)) {
    const res = await fetch('/api/candles/' + encodeURIComponent(p.pair), {cache:'no-store'});
    const hist = await res.json();
    const c = document.createElement('canvas');
    c.width = 360; c.height = 220;
    charts.appendChild(c);
    new Chart(c.getContext('2d'), {
      type: 'line',
      data: {
        labels: hist.times,
        datasets: [{ label: p.pair, data: hist.prices, borderWidth: 2, fill: false, tension: .25, pointRadius: 0 }]
      },
      options: { plugins:{legend:{display:false}}, scales:{x:{display:false}, y:{beginAtZero:false}} }
    });
  }
}
load();
</script>
</body></html>
"""

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def home(_: Request):
    return HTML.replace("{{ refresh_seconds }}", str(REFRESH_SECONDS))

@app.get("/api/top-picks", dependencies=[Depends(check_auth)])
async def api_top_picks():
    # simple “top” = highest prices (adjust later to your real ranking)
    ordered = sorted(prices.items(), key=lambda kv: kv[1] or 0, reverse=True)
    return [{"pair": p, "price": v} for p, v in ordered]

@app.get("/api/candles/{product_id}", dependencies=[Depends(check_auth)])
async def api_candles(product_id: str):
    """Return ~30 daily closes for the pair to draw a chart."""
    end = datetime.utcnow().replace(microsecond=0)
    start = end - timedelta(days=31)
    url = (
        f"{COINBASE_API}/products/{product_id}/candles"
        f"?granularity=86400&start={start.isoformat()}&end={end.isoformat()}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=15) as resp:
            data = await resp.json()
    if not isinstance(data, list):
        return {"times": [], "prices": []}
    data.sort(key=lambda r: r[0])  # [time, low, high, open, close, volume]
    times = [datetime.utcfromtimestamp(int(r[0])).strftime("%m-%d") for r in data[-30:]]
    closes = [float(r[4]) for r in data[-30:]]
    return {"times": times, "prices": closes}

@app.get("/api/health")
async def health():
    return {"status": "ok", "count": len(prices), "updated": datetime.utcnow().isoformat() + "Z"}
