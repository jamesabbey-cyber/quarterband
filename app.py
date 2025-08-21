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

# Quality filter knobs
# Liquidity (approx) = last price * 24h base volume
MIN_24H_DOLLAR_VOL = float(os.getenv("MIN_24H_DOLLAR_VOL", "20000000"))  # $20M default
# Bid/ask spread (% of mid); lower = tighter market
MAX_SPREA_
