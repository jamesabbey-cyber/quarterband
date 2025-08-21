import os
import asyncio
import aiohttp
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.requests import Request
from datetime import datetime, timedelta
import secrets

# -------- Config --------
COINBASE_API = "https://api.exchange.coinbase.com"
PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "AVAX-USD", "DOGE-USD"]
REFRESH_SECONDS = 60

# Auth
security = HTTPBasic()
APP_USER = os.getenv("APP_USER", "admin")
APP_PASS = os.getenv("APP_PASS", "password")

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, APP_USER) and
            secrets.compare_digest(credentials.password, APP_PASS)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid authentication credentials",
                            headers={"WWW-Authenticate": "Basic"})
    return True

# -------- App --------
app = FastAPI()
prices = {}

async def fetch_price(session, pair):
    url = f"{COINBASE_API}/products/{pair}/ticker"
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            return pair, float(data["price"])
    except:
        return pair, None

async def update_prices():
    global prices
    while True:
        async with aiohttp.ClientSession() as session:
            tasks = [fetch_price(session, p) for p in PAIRS]
            results = await asyncio.gather(*tasks)
            prices = {pair: price for pair, price in results if price}
        await asyncio.sleep(REFRESH_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(update_prices())

# -------- Routes --------
HTML_TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><title>QuarterBand 70/30</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="{{ refresh_seconds }}">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head><body style="font-family:sans-serif; background:#111; color:#eee; text-align:center;">
<h1>QuarterBand 70/30 Tracker</h1>
<div id="cards" style="display:flex;flex-wrap:wrap;justify-content:center;"></div>

<h2 style="margin-top:40px;">Performance Charts</h2>
<div id="charts" style="display:flex;flex-wrap:wrap;justify-content:center;"></div>

<script>
async function loadData() {
  let res = await fetch("/api/top-picks");
  let data = await res.json();

  let cardsDiv = document.getElementById("cards");
  cardsDiv.innerHTML = "";
  data.forEach(p => {
    let div = document.createElement("div");
    div.style = "background:#222; margin:10px; padding:20px; border-radius:10px; min-width:150px;";
    div.innerHTML = "<h3>"+p.pair+"</h3><p>$"+p.price.toFixed(2)+"</p>";
    cardsDiv.appendChild(div);
  });

  let chartsDiv = document.getElementById("charts");
  chartsDiv.innerHTML = "";
  for (let p of data) {
    let c = document.createElement("canvas");
    c.width=300; c.height=200;
    chartsDiv.appendChild(c);
    let histRes = await fetch("/api/candles/"+p.pair);
    let hist = await histRes.json();
    new Chart(c, {
      type: 'line',
      data: {
        labels: hist.times,
        datasets: [{
          label: p.pair,
          data: hist.prices,
          borderColor: 'rgb(75,192,192)',
          fill: false,
          tension: 0.1
        }]
      },
      options: {scales:{x:{display:false}, y:{beginAtZero:false}}}
    });
  }
}
loadData();
</script>
</body></html>
"""

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
async def home(request: Request):
    return HTML_TEMPLATE.replace("{{ refresh_seconds }}", str(REFRESH_SECONDS))

@app.get("/api/top-picks", dependencies=[Depends(check_auth)])
async def get_top_picks():
    sorted_pairs = sorted(prices.items(), key=lambda x: x[1] if x[1] else 0, reverse=True)
    top = sorted_pairs[:3]
    return [{"pair": p, "price": v} for p,v in top]

@app.get("/api/candles/{product_id}", dependencies=[Depends(check_auth)])
async def get_candles(product_id: str):
    end = datetime.utcnow()
    start = end - timedelta(days=30)
    url = f"{COINBASE_API}/products/{product_id}/candles?granularity=86400&start={start.isoformat()}&end={end.isoformat()}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    if not isinstance(data, list):
        return JSONResponse(content={"times":[],"prices":[]})
    data.sort(key=lambda x: x[0])
    times = [datetime.utcfromtimestamp(d[0]).strftime("%m-%d") for d in data]
    closes = [d[4] for d in data]
    return {"times": times, "prices": closes}
