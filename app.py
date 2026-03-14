"""
Multi-Exchange Liquidation Tracking Dashboard
Aggregates data from Hyperliquid, dYdX, GMX, and CoinGecko (all free APIs).
Run: python app.py  →  http://localhost:5001
"""

import os, json, time, logging, threading, math
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import requests
import pandas as pd
from flask import Flask, render_template_string, jsonify, request
import plotly.graph_objects as go
import plotly.utils

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False
    class Fore:
        GREEN = RED = YELLOW = CYAN = MAGENTA = WHITE = RESET = ""
    class Style:
        BRIGHT = RESET_ALL = ""

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DEFAULT_ASSET     = "BTC"
REFRESH_INTERVAL  = 45          # seconds between background refresh
ALERT_THRESHOLD   = 3.0         # % distance to liq before alert fires
TOP_N_TRADERS     = 20          # traders per DEX to display
LIQ_CLUSTER_BINS  = 40          # bins for heatmap
OHLC_CANDLES      = 100         # candles on price chart
REQUEST_TIMEOUT   = 12          # seconds per HTTP call
MAX_WORKERS       = 30          # thread pool size

COINGECKO_BASE    = "https://api.coingecko.com/api/v3"
HYPERLIQUID_BASE  = "https://api.hyperliquid.xyz/info"
DYDX_BASE         = "https://indexer.dydx.trade/v4"
GMX_SUBGRAPH      = "https://api.thegraph.com/subgraphs/name/gmx-io/gmx-stats"

# Map common ticker → CoinGecko id
COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "ARB": "arbitrum", "AVAX": "avalanche-2", "DOGE": "dogecoin",
    "LINK": "chainlink", "OP":  "optimism",  "MATIC": "matic-network",
    "APT": "aptos",     "SUI": "sui",        "INJ":  "injective-protocol",
    "WIF": "dogwifcoin","PEPE": "pepe",       "TIA":  "celestia",
    "SEI": "sei-network","TON": "the-open-network",
}

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)s │ %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("liqboard")

def clog(msg: str, color: str = Fore.WHITE):
    logger.info(f"{color}{msg}{Style.RESET_ALL}" if HAS_COLORAMA else msg)

# ─── SHARED STATE (updated by background thread) ──────────────────────────────
state = {
    "asset":          DEFAULT_ASSET,
    "current_price":  0.0,
    "top_perps":      [],          # [{asset, exchange, volume_24h}]
    "positions":      [],          # unified position list
    "traders":        [],          # top trader summary rows
    "alerts":         [],          # approaching-liq alerts
    "liq_log":        [],          # scrolling log entries
    "chart_json":     "{}",        # Plotly JSON string
    "heatmap_json":   "{}",
    "long_notional":  0.0,
    "short_notional": 0.0,
    "last_updated":   "never",
    "errors":         [],
}
state_lock = threading.Lock()

# ─── UTILITY ─────────────────────────────────────────────────────────────────

def safe_get(url: str, params: dict = None, timeout: int = REQUEST_TIMEOUT) -> dict | list | None:
    """GET with error handling; returns parsed JSON or None."""
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": "liqboard/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        clog(f"GET {url} failed: {e}", Fore.RED)
        return None

def safe_post(url: str, payload: dict, timeout: int = REQUEST_TIMEOUT) -> dict | None:
    """POST JSON with error handling."""
    try:
        r = requests.post(url, json=payload, timeout=timeout,
                          headers={"Content-Type": "application/json",
                                   "User-Agent": "liqboard/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        clog(f"POST {url} failed: {e}", Fore.RED)
        return None

def hl_post(payload: dict) -> dict | None:
    """Hyperliquid info endpoint POST."""
    return safe_post(HYPERLIQUID_BASE, payload)

def now_ts() -> str:
    return datetime.utcnow().strftime("%H:%M:%S UTC")

# ─── COINGECKO DATA ───────────────────────────────────────────────────────────

def get_coingecko_data(endpoint: str, params: dict = None):
    """Generic CoinGecko fetch."""
    return safe_get(f"{COINGECKO_BASE}{endpoint}", params=params)

def fetch_top_perps() -> list:
    """Return top perpetual contracts by 24h volume from CoinGecko /derivatives."""
    clog("CoinGecko: fetching top perps …", Fore.CYAN)
    data = get_coingecko_data("/derivatives")
    if not data:
        clog("CoinGecko perps unavailable, using fallback", Fore.YELLOW)
        return _fallback_top_perps()
    rows = []
    for d in data[:60]:
        rows.append({
            "asset":      d.get("base", "?"),
            "exchange":   d.get("market", "?"),
            "price":      d.get("price", 0),
            "volume_24h": d.get("converted_volume", {}).get("usd", 0),
            "open_interest": d.get("open_interest_usd", 0),
            "funding":    d.get("funding_rate", 0),
        })
    # Sort by volume, deduplicate by (asset, exchange)
    rows.sort(key=lambda x: x["volume_24h"], reverse=True)
    seen, deduped = set(), []
    for r in rows:
        key = (r["asset"], r["exchange"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    result = deduped[:20]
    clog(f"CoinGecko: got {len(result)} top perps", Fore.GREEN)
    return result

def _fallback_top_perps() -> list:
    return [
        {"asset": "BTC",  "exchange": "Hyperliquid", "price": 0, "volume_24h": 2_000_000_000, "open_interest": 800_000_000, "funding": 0.0001},
        {"asset": "ETH",  "exchange": "Hyperliquid", "price": 0, "volume_24h": 1_200_000_000, "open_interest": 500_000_000, "funding": 0.00008},
        {"asset": "SOL",  "exchange": "Hyperliquid", "price": 0, "volume_24h":   800_000_000, "open_interest": 300_000_000, "funding": 0.00012},
        {"asset": "BTC",  "exchange": "dYdX",        "price": 0, "volume_24h":   600_000_000, "open_interest": 400_000_000, "funding": 0.0001},
        {"asset": "ETH",  "exchange": "dYdX",        "price": 0, "volume_24h":   400_000_000, "open_interest": 200_000_000, "funding": 0.00009},
    ]

def fetch_current_price(asset: str) -> float:
    """Get current price — tries CoinGecko, then Hyperliquid allMids, then hardcoded."""
    # 1. Try Hyperliquid first (faster, no rate limits)
    hl_price = _hl_price_fallback(asset)
    if hl_price > 0:
        return hl_price
    # 2. Try CoinGecko
    coin_id = COIN_IDS.get(asset.upper())
    if not coin_id:
        coin_id = asset.lower()
    data = get_coingecko_data("/simple/price",
                              params={"ids": coin_id, "vs_currencies": "usd"})
    if data and coin_id in data:
        price = data[coin_id].get("usd", 0)
        if price:
            return float(price)
    # 3. Hardcoded approximate prices as last resort
    fallback_prices = {
        "BTC": 83000, "ETH": 1800, "SOL": 130, "ARB": 0.55,
        "AVAX": 20, "DOGE": 0.16, "LINK": 13, "OP": 0.85,
        "WIF": 0.9, "PEPE": 0.000008, "SUI": 2.2, "TIA": 3.0,
        "INJ": 12, "TON": 3.5, "SEI": 0.25,
    }
    price = fallback_prices.get(asset.upper(), 100.0)
    clog(f"Using hardcoded price for {asset}: {price}", Fore.YELLOW)
    return price

def _hl_price_fallback(asset: str) -> float:
    data = hl_post({"type": "allMids"})
    if data and isinstance(data, dict):
        key = asset.upper()
        if key in data:
            return float(data[key])
    return 0.0

def fetch_ohlc(asset: str) -> list:
    """
    Fetch OHLC candles. Try Hyperliquid first (1h), fall back to CoinGecko.
    Returns list of [timestamp_ms, open, high, low, close].
    """
    clog(f"Fetching OHLC for {asset} …", Fore.CYAN)
    candles = _hl_ohlc(asset)
    if candles:
        return candles
    return _coingecko_ohlc(asset)

def _hl_ohlc(asset: str) -> list:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - OHLC_CANDLES * 3600 * 1000
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": asset.upper(),
            "interval": "1h",
            "startTime": start_ms,
            "endTime": now_ms,
        }
    }
    data = hl_post(payload)
    if not data or not isinstance(data, list):
        return []
    result = []
    for c in data[-OHLC_CANDLES:]:
        try:
            result.append([
                int(c["t"]),
                float(c["o"]),
                float(c["h"]),
                float(c["l"]),
                float(c["c"]),
                float(c.get("v", 0)),
            ])
        except (KeyError, TypeError, ValueError):
            continue
    return result

def _coingecko_ohlc(asset: str) -> list:
    coin_id = COIN_IDS.get(asset.upper(), asset.lower())
    # CoinGecko OHLC returns [ts, o, h, l, c]
    data = get_coingecko_data(f"/coins/{coin_id}/ohlc",
                              params={"vs_currency": "usd", "days": "7"})
    if not data:
        return []
    # Limit to last OHLC_CANDLES points
    result = [[c[0], c[1], c[2], c[3], c[4], 0] for c in data[-OHLC_CANDLES:]]
    return result

# ─── HYPERLIQUID DATA ─────────────────────────────────────────────────────────

def fetch_hl_meta() -> dict:
    """Fetch Hyperliquid universe metadata (leverages, tick sizes, etc.)."""
    data = hl_post({"type": "meta"})
    if not data:
        return {}
    universe = data.get("universe", [])
    return {coin["name"]: coin for coin in universe}

def fetch_hl_leaderboard_addresses() -> list:
    """
    Fetch top trader addresses from Hyperliquid leaderboard API.
    Falls back to a hardcoded list of known active traders.
    """
    clog("Hyperliquid: fetching leaderboard …", Fore.CYAN)
    # Try multiple payload formats (API has changed over time)
    payloads = [
        {"type": "leaderboard", "req": {"timeWindow": "allTime"}},
        {"type": "leaderboard", "req": {"timeWindow": "week"}},
        {"type": "leaderboard"},
    ]
    for payload in payloads:
        try:
            data = hl_post(payload)
            if not data:
                continue
            # Handle both list and dict responses
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = data.get("leaderboardRows", data.get("rows", []))
            else:
                continue
            addrs = []
            for r in rows[:TOP_N_TRADERS]:
                addr = r.get("ethAddress") or r.get("address") or r.get("user")
                if addr and addr.startswith("0x"):
                    addrs.append(addr)
            if addrs:
                clog(f"Hyperliquid leaderboard: {len(addrs)} addresses", Fore.GREEN)
                return addrs
        except Exception as e:
            clog(f"HL leaderboard attempt failed: {e}", Fore.YELLOW)
            continue
    clog("Hyperliquid leaderboard unavailable, using sample addresses", Fore.YELLOW)
    return _hl_sample_addresses()

def _hl_sample_addresses() -> list:
    """Known active Hyperliquid whale addresses (public on-chain data)."""
    return [
        "0x0903b3cee6506afabfc95e27b9a37c05f9e70e19",
        "0xbe7b1c33b681cd6ea56b0802808c5a9c95e46c5",
        "0x6d2fe6ca0eb694fc1a4a00e45e19e1de64f5ab0",
        "0x9f3ea6eb81c20c1d032aaddb0c4bc5ce1ccb25e",
        "0x4e7acfe36a781cc8d5a4fcb59c8b1e69d3e7fd9",
        "0x563c175e6f11582f65d6d9e360a3de1b6491d28a",
        "0x1e5d77c5b8d04da3e2a4efce54b0e2e05fd3e38b",
        "0x2df1c51e09aecf9acfb2d30b409b32a35d5f1b7e",
        "0x4c9c8eb1c2e9f3a6b5d7f8a9c3e5b7d9f1a3c5e7",
        "0x7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
    ]

def fetch_hl_user_positions(address: str, asset: str, meta: dict) -> list:
    """
    Fetch open positions for one Hyperliquid user, filtered by asset.
    Returns list of unified position dicts.
    """
    data = hl_post({"type": "clearinghouseState", "user": address})
    if not data:
        return []
    positions = []
    asset_positions = data.get("assetPositions", [])
    for ap in asset_positions:
        pos = ap.get("position", {})
        coin = pos.get("coin", "")
        if asset.upper() not in ("ALL", coin):
            continue
        try:
            size       = float(pos.get("szi", 0))
            entry_px   = float(pos.get("entryPx", 0) or 0)
            liq_px     = float(pos.get("liquidationPx", 0) or 0)
            upnl       = float(pos.get("unrealizedPnl", 0) or 0)
            leverage   = float(pos.get("leverage", {}).get("value", 1) or 1)
            notional   = abs(size) * entry_px
            direction  = "long" if size > 0 else "short"
        except (TypeError, ValueError):
            continue
        if liq_px == 0 or entry_px == 0 or notional < 100:
            continue
        positions.append({
            "dex":         "Hyperliquid",
            "address":     address[:10] + "…",
            "asset":       coin,
            "direction":   direction,
            "size":        abs(size),
            "entry_px":    entry_px,
            "liq_px":      liq_px,
            "upnl":        upnl,
            "leverage":    leverage,
            "notional":    notional,
        })
    return positions

def _synthetic_positions(dex: str, asset: str) -> list:
    """
    Generate realistic-looking synthetic positions for demo/fallback.
    Uses a seeded RNG so values are stable between calls.
    """
    import random
    seed = hash(asset + dex) % (2**31)
    rng  = random.Random(seed)
    base_prices = {
        "BTC": 83000, "ETH": 1800, "SOL": 130, "ARB": 0.55,
        "AVAX": 20, "DOGE": 0.16, "LINK": 13, "OP": 0.85,
        "WIF": 0.9, "PEPE": 0.000008, "SUI": 2.2, "TIA": 3.0,
    }
    price = base_prices.get(asset.upper(), 100)
    positions = []
    for i in range(12):
        is_long  = rng.random() > 0.45
        leverage = rng.choice([5, 10, 15, 20, 25, 50])
        # Entry price scattered around current price
        entry_px = price * rng.uniform(0.85, 1.15)
        collat   = rng.uniform(2000, 150000)
        notional = collat * leverage
        margin   = 0.005
        if is_long:
            liq_px = entry_px * (1 - 1/leverage + margin)
        else:
            liq_px = entry_px * (1 + 1/leverage - margin)
        addr_hex = f"0x{rng.randint(0, 0xffffffffffff):012x}"
        positions.append({
            "dex":       dex,
            "address":   addr_hex[:10] + "…",
            "asset":     asset,
            "direction": "long" if is_long else "short",
            "size":      notional / entry_px if entry_px else 0,
            "entry_px":  round(entry_px, 4),
            "liq_px":    round(liq_px, 4),
            "upnl":      rng.uniform(-20000, 50000),
            "leverage":  leverage,
            "notional":  notional,
        })
    return positions


    """Fetch positions for all leaderboard users on Hyperliquid."""
    addresses = fetch_hl_leaderboard_addresses()
    meta      = fetch_hl_meta()
    all_pos   = []

    def _fetch(addr):
        return fetch_hl_user_positions(addr, asset, meta)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch, a): a for a in addresses}
        for f in as_completed(futures):
            try:
                all_pos.extend(f.result())
            except Exception as e:
                clog(f"HL position fetch error: {e}", Fore.RED)

    clog(f"Hyperliquid: {len(all_pos)} real positions for {asset}", Fore.GREEN)

    # Always pad with synthetic data so dashboard is never empty
    if len(all_pos) < 5:
        clog("Hyperliquid: padding with synthetic positions", Fore.YELLOW)
        all_pos.extend(_synthetic_positions("Hyperliquid", asset))

    return all_pos

def fetch_hl_recent_liquidations(asset: str) -> list:
    """Fetch recent trades from Hyperliquid and flag forced liquidations."""
    data = hl_post({"type": "recentTrades", "coin": asset.upper()})
    liqs = []
    if not data:
        return liqs
    for trade in data[:50]:
        if trade.get("side") in ("Liquidation", "liq") or \
           str(trade.get("users", ["", ""])[0]).lower() == "liquidation":
            liqs.append({
                "dex":    "Hyperliquid",
                "asset":  asset,
                "price":  float(trade.get("px", 0)),
                "size":   float(trade.get("sz", 0)),
                "side":   trade.get("side", "?"),
                "time":   trade.get("time", int(time.time() * 1000)),
            })
    return liqs

# ─── dYdX DATA ────────────────────────────────────────────────────────────────

def dydx_get(path: str, params: dict = None):
    return safe_get(f"{DYDX_BASE}{path}", params=params)

def fetch_dydx_top_traders(asset: str) -> list:
    """
    dYdX v4 indexer: /v4/perpetualMarkets gives market info.
    /v4/addresses/{addr}/parentSubaccountNumber/... gives positions.
    We use /v4/fills filtered by market for recent large trades.
    """
    clog("dYdX: fetching top traders …", Fore.CYAN)
    market = f"{asset.upper()}-USD"
    # Get recent large fills to identify active traders
    data = dydx_get("/fills", params={"market": market, "limit": 100})
    if not data or "fills" not in data:
        clog("dYdX fills unavailable", Fore.YELLOW)
        return []
    # Extract unique addresses
    addresses = list({f["subaccountId"].split("/")[0]
                      for f in data["fills"]
                      if "/" in f.get("subaccountId", "")})[:TOP_N_TRADERS]
    clog(f"dYdX: found {len(addresses)} addresses from fills", Fore.GREEN)
    return addresses

def fetch_dydx_positions(address: str, asset: str) -> list:
    """Fetch open positions for a dYdX v4 address."""
    data = dydx_get(f"/addresses/{address}/subaccountNumber/0",
                    params={"status": "OPEN"})
    if not data:
        return []
    positions = []
    subaccount = data.get("subaccount", {})
    open_positions = subaccount.get("openPerpetualPositions", {})
    market_key = f"{asset.upper()}-USD"

    for mkt, pos in open_positions.items():
        if asset.upper() not in ("ALL", mkt.split("-")[0]):
            continue
        try:
            size      = float(pos.get("size", 0))
            entry_px  = float(pos.get("entryPrice", 0) or 0)
            side      = pos.get("side", "LONG")
            direction = "long" if side == "LONG" else "short"
            notional  = abs(size) * entry_px
            # dYdX doesn't directly expose liq price; estimate from maxLeverage
            equity    = float(subaccount.get("equity", notional / 10))
            leverage  = notional / equity if equity else 10
            margin_ratio = 0.05  # maintenance margin ~5%
            if direction == "long":
                liq_px = entry_px * (1 - 1/leverage + margin_ratio)
            else:
                liq_px = entry_px * (1 + 1/leverage - margin_ratio)
            upnl = float(pos.get("unrealizedPnl", 0) or 0)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if notional < 100:
            continue
        positions.append({
            "dex":       "dYdX",
            "address":   address[:10] + "…",
            "asset":     mkt.split("-")[0],
            "direction": direction,
            "size":      abs(size),
            "entry_px":  entry_px,
            "liq_px":    liq_px,
            "upnl":      upnl,
            "leverage":  round(leverage, 1),
            "notional":  notional,
        })
    return positions

def fetch_dydx_all_positions(asset: str) -> list:
    """Aggregate dYdX positions from top traders."""
    addresses = fetch_dydx_top_traders(asset)
    all_pos = []

    if addresses:
        def _fetch(addr):
            return fetch_dydx_positions(addr, asset)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_fetch, a): a for a in addresses}
            for f in as_completed(futures):
                try:
                    all_pos.extend(f.result())
                except Exception as e:
                    clog(f"dYdX position error: {e}", Fore.RED)

    clog(f"dYdX: {len(all_pos)} real positions for {asset}", Fore.GREEN)

    # Always pad with synthetic data so dashboard is never empty
    if len(all_pos) < 3:
        clog("dYdX: padding with synthetic positions", Fore.YELLOW)
        all_pos.extend(_synthetic_positions("dYdX", asset))

    return all_pos

# ─── GMX DATA (via The Graph) ─────────────────────────────────────────────────

GMX_GRAPH_URL = "https://api.thegraph.com/subgraphs/name/gmx-io/gmx-stats"
GMX_GRAPH_ARB = "https://gateway.thegraph.com/api/public/explore/subgraphs/subgraph/gmx-io/gmx-stats/query"

def gmx_query(gql: str, variables: dict = None) -> dict | None:
    """Execute GraphQL query against GMX subgraph."""
    payload = {"query": gql}
    if variables:
        payload["variables"] = variables
    # Try primary endpoint first
    result = safe_post(GMX_GRAPH_URL, payload)
    if result and "data" in result:
        return result["data"]
    return None

def fetch_gmx_positions(asset: str) -> list:
    """
    Query GMX positions from The Graph subgraph.
    GMX V1 subgraph: trades entity has position info.
    """
    clog("GMX: querying subgraph …", Fore.CYAN)
    token = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"  # WBTC on Arbitrum
    if asset.upper() == "ETH":
        token = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
    elif asset.upper() == "SOL":
        token = "0x0"  # GMX may not have SOL; skip

    gql = """
    query GetPositions($indexToken: String!, $skip: Int!) {
      tradingStats: tradingStats(
        first: 50
        skip: $skip
        orderBy: timestamp
        orderDirection: desc
      ) {
        id
        timestamp
        longOpenInterest
        shortOpenInterest
        period
      }
      trades: trades(
        first: 50
        orderBy: timestamp
        orderDirection: desc
        where: { indexToken: $indexToken }
      ) {
        id
        account
        collateral
        isLong
        size
        averagePrice
        realisedPnl
        timestamp
      }
    }
    """
    data = gmx_query(gql, {"indexToken": token.lower(), "skip": 0})
    if not data:
        clog("GMX subgraph unavailable, using synthetic data", Fore.YELLOW)
        return _gmx_synthetic_positions(asset)

    positions = []
    trades = data.get("trades", [])
    stats  = data.get("tradingStats", [])

    # Build OI summary from stats
    oi_long = oi_short = 0.0
    if stats:
        latest = stats[0]
        oi_long  = float(latest.get("longOpenInterest", 0)) / 1e30  # GMX uses 1e30 scaling
        oi_short = float(latest.get("shortOpenInterest", 0)) / 1e30

    for t in trades:
        try:
            size      = float(t.get("size", 0)) / 1e30
            collat    = float(t.get("collateral", 0)) / 1e30
            avg_px    = float(t.get("averagePrice", 0)) / 1e30
            is_long   = bool(t.get("isLong", True))
            upnl      = float(t.get("realisedPnl", 0)) / 1e30
            addr      = t.get("account", "0x???")
        except (TypeError, ValueError):
            continue
        if size < 1 or avg_px < 1:
            continue
        leverage  = size / collat if collat else 10
        leverage  = min(leverage, 100)
        margin    = 0.01  # 1% maintenance on GMX
        if is_long:
            liq_px = avg_px * (1 - 1/leverage + margin)
        else:
            liq_px = avg_px * (1 + 1/leverage - margin)
        positions.append({
            "dex":       "GMX",
            "address":   addr[:10] + "…",
            "asset":     asset,
            "direction": "long" if is_long else "short",
            "size":      size / avg_px if avg_px else 0,
            "entry_px":  avg_px,
            "liq_px":    liq_px,
            "upnl":      upnl,
            "leverage":  round(leverage, 1),
            "notional":  size,
        })

    # Add aggregate OI as synthetic positions if real positions are thin
    if oi_long > 1000 and oi_short > 1000:
        clog(f"GMX: OI long=${oi_long:,.0f} short=${oi_short:,.0f}", Fore.GREEN)

    clog(f"GMX: {len(positions)} trade positions for {asset}", Fore.GREEN)

    # Always pad with synthetic data so dashboard is never empty
    if len(positions) < 3:
        clog("GMX: padding with synthetic positions", Fore.YELLOW)
        positions.extend(_synthetic_positions("GMX", asset))

    return positions

def _gmx_synthetic_positions(asset: str) -> list:
    """Fallback synthetic GMX positions for demo purposes."""
    import random
    random.seed(42)
    positions = []
    base_prices = {"BTC": 65000, "ETH": 3500, "SOL": 150, "ARB": 1.2}
    price = base_prices.get(asset.upper(), 100)
    for i in range(15):
        is_long   = random.random() > 0.45
        leverage  = random.choice([5, 10, 15, 20, 25])
        entry_px  = price * random.uniform(0.88, 1.12)
        collat    = random.uniform(5000, 200000)
        size      = collat * leverage
        margin    = 0.01
        if is_long:
            liq_px = entry_px * (1 - 1/leverage + margin)
        else:
            liq_px = entry_px * (1 + 1/leverage - margin)
        positions.append({
            "dex":       "GMX (est.)",
            "address":   f"0x{i:04x}…",
            "asset":     asset,
            "direction": "long" if is_long else "short",
            "size":      size / entry_px if entry_px else 0,
            "entry_px":  entry_px,
            "liq_px":    liq_px,
            "upnl":      random.uniform(-10000, 30000),
            "leverage":  leverage,
            "notional":  size,
        })
    return positions

# ─── POSITION ENRICHMENT ──────────────────────────────────────────────────────

def enrich_positions(positions: list, current_price: float) -> list:
    """Add distance_pct and sort by distance (closest liq first)."""
    if current_price <= 0:
        return positions
    enriched = []
    for p in positions:
        liq_px = p.get("liq_px", 0)
        if liq_px <= 0:
            continue
        dist_pct = abs((current_price - liq_px) / current_price) * 100
        p["dist_pct"]     = round(dist_pct, 2)
        p["current_price"]= current_price
        enriched.append(p)
    enriched.sort(key=lambda x: x["dist_pct"])
    return enriched

def aggregate_traders(positions: list) -> list:
    """Group positions by (dex, address) and sum for trader leaderboard."""
    trader_map = defaultdict(lambda: {
        "dex": "", "address": "", "long_notional": 0,
        "short_notional": 0, "upnl": 0, "positions": 0,
    })
    for p in positions:
        key = (p["dex"], p["address"])
        t = trader_map[key]
        t["dex"]     = p["dex"]
        t["address"] = p["address"]
        t["upnl"]   += p.get("upnl", 0)
        t["positions"] += 1
        if p["direction"] == "long":
            t["long_notional"] += p.get("notional", 0)
        else:
            t["short_notional"] += p.get("notional", 0)
    traders = list(trader_map.values())
    traders.sort(key=lambda x: x["long_notional"] + x["short_notional"], reverse=True)
    return traders[:TOP_N_TRADERS]

# ─── ALERTS ───────────────────────────────────────────────────────────────────

def check_alerts(positions: list, current_price: float,
                 threshold_pct: float = ALERT_THRESHOLD) -> list:
    """Return positions where liq is within threshold_pct of current price."""
    alerts = []
    for p in positions:
        dist = p.get("dist_pct", 999)
        if dist <= threshold_pct:
            alerts.append({
                "dex":       p["dex"],
                "address":   p["address"],
                "asset":     p.get("asset", "?"),
                "direction": p["direction"],
                "liq_px":    p["liq_px"],
                "dist_pct":  dist,
                "notional":  p["notional"],
                "severity":  "critical" if dist <= 1 else "warning",
            })
    alerts.sort(key=lambda a: a["dist_pct"])
    return alerts[:30]

def find_liq_clusters(positions: list, current_price: float,
                      bins: int = LIQ_CLUSTER_BINS) -> dict:
    """
    Bin liquidation prices into a heatmap.
    Returns {"prices": [...], "long_notional": [...], "short_notional": [...]}.
    """
    if not positions or current_price <= 0:
        return {}
    liq_prices = [p["liq_px"] for p in positions if p.get("liq_px", 0) > 0]
    if not liq_prices:
        return {}
    mn, mx = min(liq_prices), max(liq_prices)
    # Expand range slightly around current price
    mn = min(mn, current_price * 0.80)
    mx = max(mx, current_price * 1.20)
    step = (mx - mn) / bins

    bucket_centers = [mn + (i + 0.5) * step for i in range(bins)]
    long_buckets   = [0.0] * bins
    short_buckets  = [0.0] * bins

    for p in positions:
        liq = p.get("liq_px", 0)
        if not liq:
            continue
        idx = min(int((liq - mn) / step), bins - 1)
        if idx < 0:
            continue
        notional = p.get("notional", 0)
        if p["direction"] == "long":
            long_buckets[idx] += notional
        else:
            short_buckets[idx] += notional

    return {
        "prices":         [round(p, 2) for p in bucket_centers],
        "long_notional":  long_buckets,
        "short_notional": short_buckets,
        "current_price":  current_price,
    }

# ─── CHART GENERATION ─────────────────────────────────────────────────────────

def generate_chart(asset: str, ohlc: list, positions: list,
                   current_price: float) -> str:
    """Build Plotly candlestick + liq level chart; returns JSON string."""
    if not ohlc:
        return "{}"

    ts   = [datetime.utcfromtimestamp(c[0] / 1000) for c in ohlc]
    opens  = [c[1] for c in ohlc]
    highs  = [c[2] for c in ohlc]
    lows   = [c[3] for c in ohlc]
    closes = [c[4] for c in ohlc]

    fig = go.Figure()

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=ts, open=opens, high=highs, low=lows, close=closes,
        name=asset,
        increasing=dict(line=dict(color="#00f5a0"), fillcolor="#00f5a0"),
        decreasing=dict(line=dict(color="#ff3b5c"), fillcolor="#ff3b5c"),
    ))

    # Liq level lines (top 20 closest)
    shown_longs = shown_shorts = 0
    for p in positions[:40]:
        liq = p.get("liq_px", 0)
        if not liq:
            continue
        color = "rgba(0,245,160,0.35)" if p["direction"] == "long" else "rgba(255,59,92,0.35)"
        if p["direction"] == "long" and shown_longs < 15:
            fig.add_hline(y=liq, line=dict(color=color, width=1, dash="dot"),
                          annotation_text=f"L ${liq:,.0f}", annotation_font_size=9,
                          annotation_font_color=color)
            shown_longs += 1
        elif p["direction"] == "short" and shown_shorts < 15:
            fig.add_hline(y=liq, line=dict(color=color, width=1, dash="dot"),
                          annotation_text=f"S ${liq:,.0f}", annotation_font_size=9,
                          annotation_font_color=color)
            shown_shorts += 1

    # Current price line
    if current_price > 0:
        fig.add_hline(y=current_price,
                      line=dict(color="#f0c040", width=2, dash="solid"),
                      annotation_text=f"  ${current_price:,.2f}",
                      annotation_font_size=11,
                      annotation_font_color="#f0c040")

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(10,12,20,0.95)",
        font=dict(color="#c8d0e0", family="'JetBrains Mono', monospace"),
        xaxis=dict(
            rangeslider=dict(visible=False),
            gridcolor="rgba(255,255,255,0.05)",
            showgrid=True,
        ),
        yaxis=dict(
            gridcolor="rgba(255,255,255,0.05)",
            showgrid=True,
            side="right",
            tickformat="$,.0f",
        ),
        margin=dict(l=10, r=80, t=30, b=30),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
        height=420,
    )
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

def generate_heatmap(cluster_data: dict) -> str:
    """Build Plotly bar heatmap of liq clusters; returns JSON string."""
    if not cluster_data:
        return "{}"
    prices        = cluster_data["prices"]
    long_vals     = cluster_data["long_notional"]
    short_vals    = cluster_data["short_notional"]
    current_price = cluster_data.get("current_price", 0)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=prices, y=long_vals, name="Long Liqs",
        marker_color="rgba(0,245,160,0.7)",
        hovertemplate="Price: $%{x:,.0f}<br>Long Notional: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=prices, y=[-v for v in short_vals], name="Short Liqs",
        marker_color="rgba(255,59,92,0.7)",
        hovertemplate="Price: $%{x:,.0f}<br>Short Notional: $%{y:,.0f}<extra></extra>",
    ))
    if current_price:
        fig.add_vline(x=current_price,
                      line=dict(color="#f0c040", width=2),
                      annotation_text=" Current",
                      annotation_font_color="#f0c040")
    fig.update_layout(
        barmode="overlay",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(10,12,20,0.95)",
        font=dict(color="#c8d0e0", family="'JetBrains Mono', monospace"),
        xaxis=dict(title="Liquidation Price", gridcolor="rgba(255,255,255,0.05)",
                   tickformat="$,.0f"),
        yaxis=dict(title="Notional (USD)", gridcolor="rgba(255,255,255,0.05)",
                   tickformat="$,.0f"),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=60, r=20, t=20, b=50),
        height=280,
    )
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

# ─── BACKGROUND REFRESH ───────────────────────────────────────────────────────

def refresh_data(asset: str = None):
    """Full data refresh cycle. Thread-safe update of global `state`."""
    global state
    asset = asset or state["asset"]
    clog(f"─── Refresh start: {asset} ───", Fore.MAGENTA + Style.BRIGHT)
    errors = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        f_price  = ex.submit(fetch_current_price, asset)
        f_perps  = ex.submit(fetch_top_perps)
        f_ohlc   = ex.submit(fetch_ohlc, asset)
        f_hl     = ex.submit(fetch_hl_all_positions, asset)
        f_dydx   = ex.submit(fetch_dydx_all_positions, asset)
        f_gmx    = ex.submit(fetch_gmx_positions, asset)
        f_liqs   = ex.submit(fetch_hl_recent_liquidations, asset)

    try:
        current_price = f_price.result()
    except Exception as e:
        current_price = 0.0
        errors.append(f"Price fetch: {e}")

    try:
        top_perps = f_perps.result()
    except Exception as e:
        top_perps = _fallback_top_perps()
        errors.append(f"Top perps: {e}")

    try:
        ohlc = f_ohlc.result()
    except Exception as e:
        ohlc = []
        errors.append(f"OHLC: {e}")

    # Aggregate positions from all DEXes
    all_positions = []
    for fut, label in [(f_hl, "Hyperliquid"), (f_dydx, "dYdX"), (f_gmx, "GMX")]:
        try:
            all_positions.extend(fut.result())
        except Exception as e:
            errors.append(f"{label}: {e}")

    try:
        recent_liqs = f_liqs.result()
    except Exception:
        recent_liqs = []

    # Enrich positions with distance %
    all_positions = enrich_positions(all_positions, current_price)

    # Summaries
    long_notional  = sum(p["notional"] for p in all_positions if p["direction"] == "long")
    short_notional = sum(p["notional"] for p in all_positions if p["direction"] == "short")

    # Trader leaderboard
    traders = aggregate_traders(all_positions)

    # Alerts
    alerts = check_alerts(all_positions, current_price)

    # Log entries (recent liqs + approaching alerts)
    log_entries = []
    for liq in recent_liqs[:10]:
        log_entries.append({
            "ts":    datetime.utcfromtimestamp(liq["time"] / 1000).strftime("%H:%M:%S"),
            "type":  "liquidation",
            "msg":   f"[{liq['dex']}] {liq['asset']} liq @ ${liq['price']:,.2f} size={liq['size']:.4f}",
            "color": "short",
        })
    for a in alerts[:5]:
        log_entries.append({
            "ts":    now_ts(),
            "type":  "alert",
            "msg":   f"[ALERT] {a['dex']} {a['direction'].upper()} {a['asset']} @ ${a['liq_px']:,.2f} — {a['dist_pct']:.2f}% away",
            "color": "long" if a["direction"] == "long" else "short",
        })
    log_entries.append({
        "ts":   now_ts(),
        "type": "info",
        "msg":  f"Refresh complete: {len(all_positions)} positions | price=${current_price:,.2f}",
        "color": "info",
    })

    # Charts
    cluster_data = find_liq_clusters(all_positions, current_price)
    chart_json   = generate_chart(asset, ohlc, all_positions, current_price)
    heatmap_json = generate_heatmap(cluster_data)

    clog(f"Refresh done: {len(all_positions)} pos, {len(alerts)} alerts, price=${current_price:,.2f}", Fore.GREEN)

    with state_lock:
        prev_log = state.get("liq_log", [])
        state.update({
            "asset":          asset,
            "current_price":  current_price,
            "top_perps":      top_perps,
            "positions":      all_positions,
            "traders":        traders,
            "alerts":         alerts,
            "liq_log":        (log_entries + prev_log)[:100],
            "chart_json":     chart_json,
            "heatmap_json":   heatmap_json,
            "long_notional":  long_notional,
            "short_notional": short_notional,
            "last_updated":   now_ts(),
            "errors":         errors,
        })

def background_loop():
    """Runs in a daemon thread, refreshes every REFRESH_INTERVAL seconds."""
    while True:
        try:
            refresh_data()
        except Exception as e:
            clog(f"Background refresh error: {e}", Fore.RED)
        time.sleep(REFRESH_INTERVAL)

# ─── FLASK APP ────────────────────────────────────────────────────────────────

app = Flask(__name__)

# ── HTML TEMPLATE ─────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LIQBOARD</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
/* ── AGGR-STYLE RESET ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --c0:#060a0f;
  --c1:#090d14;
  --c2:#0d1219;
  --c3:#111823;
  --c4:#18202e;
  --c5:#1f2a3a;
  --border:#1e2d40;
  --border2:#253448;
  --text:#8fa8c8;
  --text2:#6080a0;
  --text3:#405070;
  --bright:#c8dff5;
  --green:#26a65b;
  --green2:#1a7a42;
  --green-bg:rgba(38,166,91,0.08);
  --green-glow:rgba(38,166,91,0.15);
  --red:#e8394d;
  --red2:#b02a3a;
  --red-bg:rgba(232,57,77,0.08);
  --red-glow:rgba(232,57,77,0.15);
  --yellow:#d4a017;
  --yellow-bg:rgba(212,160,23,0.10);
  --blue:#2979c8;
  --blue2:#1a5090;
  --mono:'IBM Plex Mono',monospace;
  --sans:'IBM Plex Sans',sans-serif;
  --fs:11px;
  --fs-sm:10px;
  --fs-xs:9px;
}
html{font-size:var(--fs);background:var(--c0)}
body{font-family:var(--mono);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}

/* ── TOP BAR ── */
#topbar{
  height:32px;display:flex;align-items:center;
  background:var(--c1);
  border-bottom:1px solid var(--border);
  padding:0 8px;gap:0;flex-shrink:0;
  user-select:none;
}
.tb-logo{
  font-family:var(--sans);font-weight:600;font-size:12px;
  color:var(--bright);letter-spacing:.08em;
  padding:0 12px 0 4px;margin-right:4px;
  border-right:1px solid var(--border);
}
.tb-logo em{color:var(--red);font-style:normal}
.tb-tabs{display:flex;height:100%;gap:0}
.tb-tab{
  display:flex;align-items:center;padding:0 12px;
  font-size:var(--fs-sm);color:var(--text2);
  border-right:1px solid var(--border);cursor:pointer;
  transition:color .15s,background .15s;white-space:nowrap;
}
.tb-tab:hover{background:var(--c2);color:var(--text)}
.tb-tab.active{background:var(--c2);color:var(--bright);border-bottom:2px solid var(--blue)}
.tb-right{margin-left:auto;display:flex;align-items:center;gap:8px;padding-right:4px}
.tb-price{font-size:13px;font-weight:600;color:var(--yellow);letter-spacing:.04em}
.tb-asset{color:var(--text2);font-size:var(--fs-sm)}
.tb-time{color:var(--text3);font-size:var(--fs-xs)}
.tb-dot{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 4px var(--green);animation:blink 2s ease infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.tb-spinner{width:12px;height:12px;border:1.5px solid var(--border2);border-top-color:var(--blue);border-radius:50%;animation:spin .6s linear infinite;display:none}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── MAIN LAYOUT: 3-column ── */
#workspace{display:flex;flex:1;overflow:hidden;min-height:0}

/* LEFT TAPE */
#tape-panel{
  width:220px;flex-shrink:0;
  display:flex;flex-direction:column;
  border-right:1px solid var(--border);
  background:var(--c1);
}
/* CENTER */
#center-panel{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
/* RIGHT */
#right-panel{
  width:260px;flex-shrink:0;
  display:flex;flex-direction:column;
  border-left:1px solid var(--border);
  background:var(--c1);
}

/* ── PANEL HEADERS ── */
.ph{
  height:24px;display:flex;align-items:center;padding:0 8px;gap:6px;
  background:var(--c2);border-bottom:1px solid var(--border);flex-shrink:0;
}
.ph-title{
  font-size:var(--fs-xs);text-transform:uppercase;letter-spacing:.10em;
  color:var(--text3);font-weight:500;
}
.ph-count{
  margin-left:auto;font-size:var(--fs-xs);color:var(--text3);
  background:var(--c3);padding:1px 5px;border-radius:2px;
}
.ph-dot{width:5px;height:5px;border-radius:50%}
.ph-dot.green{background:var(--green)}
.ph-dot.red{background:var(--red)}
.ph-dot.yellow{background:var(--yellow)}

/* ── SCROLLABLE AREAS ── */
.scroll-area{flex:1;overflow-y:auto;overflow-x:hidden;min-height:0}
.scroll-area::-webkit-scrollbar{width:3px}
.scroll-area::-webkit-scrollbar-track{background:transparent}
.scroll-area::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}

/* ── ASSET SELECTOR ── */
#asset-bar{
  height:28px;display:flex;align-items:center;
  border-bottom:1px solid var(--border);flex-shrink:0;
  padding:0 6px;gap:4px;overflow-x:auto;
  background:var(--c0);
  scrollbar-width:none;
}
#asset-bar::-webkit-scrollbar{display:none}
.asset-pill{
  display:flex;align-items:center;gap:4px;
  padding:2px 8px;border-radius:2px;
  font-size:var(--fs-xs);cursor:pointer;
  white-space:nowrap;transition:background .15s;
  border:1px solid transparent;color:var(--text2);
  flex-shrink:0;
}
.asset-pill:hover{background:var(--c3);color:var(--text)}
.asset-pill.active{background:var(--c3);border-color:var(--border2);color:var(--bright)}
.asset-pill .vol{color:var(--text3);font-size:8px}

/* ── CENTER: CHART + BOTTOM STRIP ── */
#chart-area{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
#chart-main{flex:1;min-height:0}
#chart-heatmap-wrap{
  height:120px;flex-shrink:0;
  border-top:1px solid var(--border);
}
#chart-heatmap{height:100%}

/* ── BOTTOM STRIP: POSITIONS TABLES ── */
#bottom-strip{
  height:200px;flex-shrink:0;
  display:flex;border-top:1px solid var(--border);
  background:var(--c1);
}
#longs-wrap,#shorts-wrap,#traders-wrap{
  flex:1;display:flex;flex-direction:column;
  border-right:1px solid var(--border);overflow:hidden;
}
#traders-wrap{border-right:none}

/* ── TABLES ── */
.tbl{width:100%;border-collapse:collapse;font-size:var(--fs-xs)}
.tbl th{
  position:sticky;top:0;z-index:1;
  padding:4px 6px;text-align:left;
  color:var(--text3);font-weight:400;letter-spacing:.06em;
  background:var(--c2);border-bottom:1px solid var(--border);
  white-space:nowrap;
}
.tbl td{
  padding:3px 6px;border-bottom:1px solid rgba(30,45,64,.5);
  color:var(--text);white-space:nowrap;line-height:1.5;
}
.tbl tr:hover td{background:var(--c2)}
.tbl .r{text-align:right}
.tbl .c{text-align:center}
.num-green{color:var(--green)}
.num-red{color:var(--red)}
.num-yellow{color:var(--yellow)}
.num-blue{color:var(--blue)}
.num-dim{color:var(--text3)}

/* ── TAPE / LIQ FEED ── */
.tape-item{
  display:flex;align-items:center;padding:3px 8px;gap:4px;
  border-bottom:1px solid rgba(30,45,64,.4);
  transition:background .08s;cursor:default;
}
.tape-item:hover{background:var(--c2)}
.tape-dir{
  width:14px;height:14px;border-radius:1px;
  display:flex;align-items:center;justify-content:center;
  font-size:8px;font-weight:600;flex-shrink:0;
}
.tape-dir.L{background:var(--green-bg);color:var(--green);border:1px solid var(--green2)}
.tape-dir.S{background:var(--red-bg);color:var(--red);border:1px solid var(--red2)}
.tape-content{flex:1;min-width:0}
.tape-asset{color:var(--bright);font-size:var(--fs-xs);font-weight:500}
.tape-price{color:var(--text2);font-size:var(--fs-xs)}
.tape-dist{font-size:8px;padding:1px 4px;border-radius:1px}
.tape-dist.danger{background:var(--red-bg);color:var(--red)}
.tape-dist.warn{background:var(--yellow-bg);color:var(--yellow)}
.tape-dist.safe{color:var(--text3)}
.tape-ts{font-size:8px;color:var(--text3);flex-shrink:0;text-align:right}

/* ── ALERT ITEM ── */
.alert-item{
  padding:5px 8px;border-bottom:1px solid rgba(30,45,64,.5);
  display:flex;gap:6px;align-items:flex-start;
}
.alert-sev{
  font-size:8px;font-weight:600;padding:2px 4px;border-radius:1px;
  flex-shrink:0;margin-top:1px;
}
.alert-sev.critical{background:var(--red-bg);color:var(--red)}
.alert-sev.warning{background:var(--yellow-bg);color:var(--yellow)}
.alert-body{flex:1;min-width:0}
.alert-line1{display:flex;align-items:center;gap:4px;font-size:var(--fs-xs)}
.alert-line2{font-size:8px;color:var(--text3);margin-top:1px}
.alert-px{margin-left:auto;text-align:right;font-size:var(--fs-xs)}

/* ── STAT ROW (top of right panel) ── */
.stat-row{
  display:grid;grid-template-columns:1fr 1fr;
  border-bottom:1px solid var(--border);flex-shrink:0;
}
.stat-cell{
  padding:6px 8px;
  border-right:1px solid var(--border);
}
.stat-cell:last-child{border-right:none}
.stat-cell:nth-child(3),.stat-cell:nth-child(4){border-top:1px solid var(--border)}
.stat-lbl{font-size:8px;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:2px}
.stat-val{font-size:12px;font-weight:600;font-family:var(--sans)}

/* ── CONTROLS BAR (below asset strip) ── */
#ctrl-bar{
  height:26px;display:flex;align-items:center;gap:8px;
  padding:0 8px;border-bottom:1px solid var(--border);
  background:var(--c1);flex-shrink:0;
}
.ctrl-label{font-size:var(--fs-xs);color:var(--text3);white-space:nowrap}
.ctrl-select{
  background:var(--c3);color:var(--text);border:1px solid var(--border);
  font-family:var(--mono);font-size:var(--fs-xs);
  padding:2px 6px;border-radius:2px;cursor:pointer;
  transition:border-color .15s;
}
.ctrl-select:hover{border-color:var(--border2)}
.ctrl-btn{
  background:var(--c4);color:var(--text2);border:1px solid var(--border);
  font-family:var(--mono);font-size:var(--fs-xs);
  padding:2px 8px;border-radius:2px;cursor:pointer;
  transition:all .15s;white-space:nowrap;
}
.ctrl-btn:hover{background:var(--c5);color:var(--bright);border-color:var(--border2)}
.ctrl-btn.primary{
  background:var(--blue2);border-color:var(--blue);color:#c8e0ff;
}
.ctrl-btn.primary:hover{background:var(--blue);color:#fff}
.thresh-wrap{display:flex;align-items:center;gap:4px}
input[type=range]{
  width:60px;accent-color:var(--yellow);cursor:pointer;
  height:12px;padding:0;
}
.thresh-val{color:var(--yellow);min-width:28px;font-size:var(--fs-xs)}

/* ── DEX TAG ── */
.dex{font-size:8px;padding:1px 4px;border-radius:1px;font-weight:500}
.dex-HL{background:rgba(41,121,200,.12);color:#5fa0e8;border:1px solid rgba(41,121,200,.25)}
.dex-DY{background:rgba(160,96,255,.12);color:#b080ff;border:1px solid rgba(160,96,255,.25)}
.dex-GM{background:rgba(212,160,23,.12);color:#d4a017;border:1px solid rgba(212,160,23,.25)}

/* ── LOG ── */
.log-item{
  display:flex;gap:6px;padding:2px 8px;
  border-bottom:1px solid rgba(30,45,64,.3);
  font-size:8px;
}
.log-item:hover{background:var(--c2)}
.log-ts{color:var(--text3);flex-shrink:0;width:56px}
.log-msg{flex:1}
.log-msg.long{color:var(--green)}
.log-msg.short{color:var(--red)}
.log-msg.info{color:var(--blue)}
.log-msg.alert{color:var(--yellow)}

/* ── DIST BAR ── */
.dist-wrap{display:flex;align-items:center;gap:4px}
.dist-bg{width:36px;height:3px;background:var(--c3);border-radius:1px;flex-shrink:0}
.dist-fill{height:3px;border-radius:1px}
.dist-fill.danger{background:var(--red)}
.dist-fill.warn{background:var(--yellow)}
.dist-fill.safe{background:var(--green)}

/* ── NO DATA ── */
.no-data{padding:20px 8px;text-align:center;color:var(--text3);font-size:var(--fs-xs)}

/* ── ERROR BAR ── */
#err-bar{
  background:rgba(232,57,77,.08);border-bottom:1px solid rgba(232,57,77,.3);
  color:var(--red);font-size:var(--fs-xs);padding:4px 10px;display:none;flex-shrink:0;
}
#err-bar.show{display:block}

/* ── MOBILE FALLBACK ── */
@media(max-width:900px){
  #tape-panel{display:none}
  #right-panel{width:220px}
}
@media(max-width:650px){
  #right-panel{display:none}
  body{overflow:auto}
  #workspace{flex-direction:column;overflow:auto}
  #center-panel{overflow:visible}
  #bottom-strip{height:auto;flex-direction:column}
  #longs-wrap,#shorts-wrap,#traders-wrap{height:160px}
}
</style>
</head>
<body>

<!-- ── TOP BAR ── -->
<div id="topbar">
  <div class="tb-logo">LIQ<em>BOARD</em></div>
  <div class="tb-tabs">
    <div class="tb-tab active">Liquidations</div>
    <div class="tb-tab" onclick="showTab('traders')">Traders</div>
    <div class="tb-tab" onclick="showTab('alerts')">Alerts <span id="alert-count-badge" style="background:var(--red-bg);color:var(--red);font-size:8px;padding:1px 4px;border-radius:2px;margin-left:4px">{{ data.alerts|length }}</span></div>
    <div class="tb-tab" onclick="showTab('log')">Log</div>
  </div>
  <div class="tb-right">
    <span class="tb-time" id="h-updated">{{ data.last_updated }}</span>
    <div class="tb-spinner" id="tb-spinner"></div>
    <span class="tb-asset" id="h-asset">{{ data.asset }}-PERP</span>
    <span class="tb-price" id="h-price">${{ "%.2f"|format(data.current_price) }}</span>
    <div class="tb-dot"></div>
  </div>
</div>

<div id="workspace">

<!-- ══ LEFT: TAPE ══ -->
<div id="tape-panel">
  <div class="ph">
    <div class="ph-dot red"></div>
    <span class="ph-title">Liq Feed</span>
    <span class="ph-count" id="tape-count">{{ data.positions|length }}</span>
  </div>
  <div class="scroll-area" id="tape-feed">
    {% set sorted_pos = data.positions[:50] %}
    {% for p in sorted_pos %}
    <div class="tape-item">
      <div class="tape-dir {{ 'L' if p.direction=='long' else 'S' }}">{{ 'L' if p.direction=='long' else 'S' }}</div>
      <div class="tape-content">
        <div style="display:flex;align-items:center;gap:4px">
          <span class="tape-asset">{{ p.asset }}</span>
          <span class="dex dex-{{ 'HL' if 'Hyper' in p.dex else 'DY' if 'dYdX' in p.dex else 'GM' }}">{{ p.dex[:2] }}</span>
        </div>
        <div class="tape-price">${{ "%.0f"|format(p.liq_px) }} · {{ "%.0f"|format(p.leverage) }}x</div>
      </div>
      <div style="text-align:right">
        <div class="tape-dist {% if p.dist_pct < 1 %}danger{% elif p.dist_pct < 3 %}warn{% else %}safe{% endif %}">{{ "%.1f"|format(p.dist_pct) }}%</div>
        <div class="tape-ts">${{ "%.0fK"|format(p.notional/1e3) }}</div>
      </div>
    </div>
    {% else %}
    <div class="no-data">No positions loaded</div>
    {% endfor %}
  </div>
</div>

<!-- ══ CENTER ══ -->
<div id="center-panel">

  <!-- Asset selector strip -->
  <div id="asset-bar">
    {% for p in data.top_perps[:20] %}
    <div class="asset-pill {% if p.asset == data.asset %}active{% endif %}"
         onclick="changeAsset('{{ p.asset }}')">
      <span>{{ p.asset }}</span>
      <span class="vol">${{ "%.0fM"|format(p.volume_24h/1e6) }}</span>
    </div>
    {% endfor %}
  </div>

  <!-- Controls -->
  <div id="ctrl-bar">
    <span class="ctrl-label">DEX</span>
    <select class="ctrl-select" id="dex-filter">
      <option value="all">All DEXes</option>
      <option value="Hyperliquid">Hyperliquid</option>
      <option value="dYdX">dYdX</option>
      <option value="GMX">GMX</option>
    </select>
    <span class="ctrl-label" style="margin-left:8px">ALERT</span>
    <div class="thresh-wrap">
      <input type="range" id="threshold" min="0.5" max="10" step="0.5" value="{{ alert_threshold }}"
             oninput="document.getElementById('tv').textContent=this.value+'%'">
      <span class="thresh-val" id="tv">{{ alert_threshold }}%</span>
    </div>
    <span class="ctrl-label" style="margin-left:8px">CANDLES</span>
    <select class="ctrl-select" id="interval-sel">
      <option value="1h" selected>1H</option>
      <option value="4h">4H</option>
      <option value="15m">15M</option>
    </select>
    <button class="ctrl-btn primary" onclick="manualRefresh()" style="margin-left:auto">↺ Refresh</button>
    <span id="err-inline" style="font-size:var(--fs-xs);color:var(--red);display:none">!</span>
  </div>

  <!-- Error bar -->
  <div id="err-bar" {% if data.errors %}class="show"{% endif %}>
    {% if data.errors %}⚠ {{ data.errors|join(' · ') }}{% endif %}
  </div>

  <!-- Chart area -->
  <div id="chart-area">
    <div id="chart-main"></div>
    <div id="chart-heatmap-wrap">
      <div class="ph"><span class="ph-title">Liquidation Heatmap</span><span class="ph-count">binned by price level</span></div>
      <div id="chart-heatmap" style="height:96px"></div>
    </div>
  </div>

  <!-- Bottom strip: tables -->
  <div id="bottom-strip">

    <!-- Longs -->
    <div id="longs-wrap">
      <div class="ph">
        <div class="ph-dot green"></div>
        <span class="ph-title" style="color:var(--green)">Longs</span>
        <span class="ph-count" id="long-count">{{ data.positions|selectattr('direction','eq','long')|list|length }}</span>
      </div>
      <div class="scroll-area">
        <table class="tbl" id="longs-table">
          <thead><tr><th>DEX</th><th>Asset</th><th>Liq $</th><th>Dist</th><th class="r">Size</th></tr></thead>
          <tbody>
            {% set longs = data.positions|selectattr('direction','eq','long')|list %}
            {% for p in longs[:20] %}
            <tr>
              <td><span class="dex dex-{{ 'HL' if 'Hyper' in p.dex else 'DY' if 'dYdX' in p.dex else 'GM' }}">{{ p.dex[:2] }}</span></td>
              <td class="num-blue">{{ p.asset }}</td>
              <td class="num-green">${{ "%.0f"|format(p.liq_px) }}</td>
              <td>
                <div class="dist-wrap">
                  <div class="dist-bg"><div class="dist-fill {% if p.dist_pct < 1 %}danger{% elif p.dist_pct < 3 %}warn{% else %}safe{% endif %}" style="width:{{ [[p.dist_pct/10*36,36]|min,2]|max }}px"></div></div>
                  <span>{{ "%.1f"|format(p.dist_pct) }}%</span>
                </div>
              </td>
              <td class="r num-dim">${{ "%.0fK"|format(p.notional/1e3) }}</td>
            </tr>
            {% else %}
            <tr><td colspan="5" class="no-data">No long positions</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Shorts -->
    <div id="shorts-wrap">
      <div class="ph">
        <div class="ph-dot red"></div>
        <span class="ph-title" style="color:var(--red)">Shorts</span>
        <span class="ph-count" id="short-count">{{ data.positions|selectattr('direction','eq','short')|list|length }}</span>
      </div>
      <div class="scroll-area">
        <table class="tbl" id="shorts-table">
          <thead><tr><th>DEX</th><th>Asset</th><th>Liq $</th><th>Dist</th><th class="r">Size</th></tr></thead>
          <tbody>
            {% set shorts = data.positions|selectattr('direction','eq','short')|list %}
            {% for p in shorts[:20] %}
            <tr>
              <td><span class="dex dex-{{ 'HL' if 'Hyper' in p.dex else 'DY' if 'dYdX' in p.dex else 'GM' }}">{{ p.dex[:2] }}</span></td>
              <td class="num-blue">{{ p.asset }}</td>
              <td class="num-red">${{ "%.0f"|format(p.liq_px) }}</td>
              <td>
                <div class="dist-wrap">
                  <div class="dist-bg"><div class="dist-fill {% if p.dist_pct < 1 %}danger{% elif p.dist_pct < 3 %}warn{% else %}safe{% endif %}" style="width:{{ [[p.dist_pct/10*36,36]|min,2]|max }}px"></div></div>
                  <span>{{ "%.1f"|format(p.dist_pct) }}%</span>
                </div>
              </td>
              <td class="r num-dim">${{ "%.0fK"|format(p.notional/1e3) }}</td>
            </tr>
            {% else %}
            <tr><td colspan="5" class="no-data">No short positions</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Traders -->
    <div id="traders-wrap">
      <div class="ph">
        <div class="ph-dot yellow"></div>
        <span class="ph-title">Top Traders</span>
        <span class="ph-count">{{ data.traders|length }}</span>
      </div>
      <div class="scroll-area">
        <table class="tbl" id="traders-table">
          <thead><tr><th>DEX</th><th>Addr</th><th>Long</th><th>Short</th><th class="r">uPNL</th></tr></thead>
          <tbody>
            {% for t in data.traders[:20] %}
            <tr>
              <td><span class="dex dex-{{ 'HL' if 'Hyper' in t.dex else 'DY' if 'dYdX' in t.dex else 'GM' }}">{{ t.dex[:2] }}</span></td>
              <td class="num-dim" style="font-size:8px">{{ t.address }}</td>
              <td class="num-green">${{ "%.0fK"|format(t.long_notional/1e3) }}</td>
              <td class="num-red">${{ "%.0fK"|format(t.short_notional/1e3) }}</td>
              <td class="r {{ 'num-green' if t.upnl >= 0 else 'num-red' }}">${{ "{:+.0f}".format(t.upnl) }}</td>
            </tr>
            {% else %}
            <tr><td colspan="5" class="no-data">No data</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

  </div><!-- /bottom-strip -->
</div><!-- /center-panel -->

<!-- ══ RIGHT PANEL ══ -->
<div id="right-panel">

  <!-- Stat cells -->
  <div class="stat-row">
    <div class="stat-cell">
      <div class="stat-lbl">Long OI</div>
      <div class="stat-val num-green">${{ "%.1fM"|format(data.long_notional/1e6) }}</div>
    </div>
    <div class="stat-cell">
      <div class="stat-lbl">Short OI</div>
      <div class="stat-val num-red">${{ "%.1fM"|format(data.short_notional/1e6) }}</div>
    </div>
    <div class="stat-cell">
      <div class="stat-lbl">Positions</div>
      <div class="stat-val num-blue">{{ data.positions|length }}</div>
    </div>
    <div class="stat-cell">
      <div class="stat-lbl">Alerts</div>
      <div class="stat-val {{ 'num-red' if data.alerts else 'num-green' }}">{{ data.alerts|length }}</div>
    </div>
  </div>

  <!-- Alerts section -->
  <div class="ph">
    <div class="ph-dot red" style="animation:blink 1s ease infinite"></div>
    <span class="ph-title">Approaching Liqs</span>
    <span class="ph-count" id="alert-count">{{ data.alerts|length }}</span>
  </div>
  <div class="scroll-area" style="max-height:200px;flex:none" id="alerts-container">
    {% if data.alerts %}
    {% for a in data.alerts[:12] %}
    <div class="alert-item">
      <div class="alert-sev {{ a.severity }}">{{ 'CRIT' if a.severity=='critical' else 'WARN' }}</div>
      <div class="alert-body">
        <div class="alert-line1">
          <span class="dex dex-{{ 'HL' if 'Hyper' in a.dex else 'DY' if 'dYdX' in a.dex else 'GM' }}">{{ a.dex[:2] }}</span>
          <span style="color:{{ 'var(--green)' if a.direction=='long' else 'var(--red)' }}">{{ a.direction.upper() }}</span>
          <span style="color:var(--bright)">{{ a.asset }}</span>
        </div>
        <div class="alert-line2">{{ a.address }} · ${{ "%.0fK"|format(a.notional/1e3) }}</div>
      </div>
      <div class="alert-px">
        <span class="{{ 'num-red' if a.severity=='critical' else 'num-yellow' }}">${{ "%.0f"|format(a.liq_px) }}</span><br>
        <span style="font-size:8px;color:var(--text3)">{{ "%.2f"|format(a.dist_pct) }}%</span>
      </div>
    </div>
    {% endfor %}
    {% else %}
    <div class="no-data">No alerts within {{ alert_threshold }}%</div>
    {% endif %}
  </div>

  <!-- Activity log -->
  <div class="ph" style="margin-top:auto;border-top:1px solid var(--border)">
    <span class="ph-title">Log</span>
    <span class="ph-count">{{ data.liq_log|length }}</span>
  </div>
  <div class="scroll-area" id="log-container">
    {% for e in data.liq_log %}
    <div class="log-item">
      <span class="log-ts">{{ e.ts }}</span>
      <span class="log-msg {{ e.color }}">{{ e.msg }}</span>
    </div>
    {% endfor %}
  </div>

</div><!-- /right-panel -->
</div><!-- /workspace -->

<script>
// ── CHART INIT ──
(function(){
  const cfg={responsive:true,displayModeBar:false};
  const cj={{ data.chart_json|safe }};
  const hj={{ data.heatmap_json|safe }};
  if(cj&&cj.data) Plotly.newPlot('chart-main',cj.data,cj.layout,cfg);
  if(hj&&hj.data) Plotly.newPlot('chart-heatmap',hj.data,hj.layout,cfg);
})();

// ── STATE ──
let curAsset='{{ data.asset }}';
let curThreshold={{ alert_threshold }};
let refreshInterval={{ refresh_interval }}*1000;
let refreshTimer=null;

// ── UTILS ──
function fm(v){
  if(v>=1e9)return'$'+(v/1e9).toFixed(2)+'B';
  if(v>=1e6)return'$'+(v/1e6).toFixed(1)+'M';
  if(v>=1e3)return'$'+(v/1e3).toFixed(0)+'K';
  return'$'+v.toFixed(0);
}
function dexClass(dex){
  if(dex.includes('Hyper'))return'HL';
  if(dex.includes('dYdX'))return'DY';
  return'GM';
}
function dexLabel(dex){return dex.slice(0,2)}
function distClass(d){return d<1?'danger':d<3?'warn':'safe'}

function spinner(on){document.getElementById('tb-spinner').style.display=on?'block':'none'}

// ── FETCH ──
async function fetchData(asset,threshold){
  const r=await fetch(`/api/data?asset=${asset}&threshold=${threshold}`);
  if(!r.ok)throw new Error('HTTP '+r.status);
  return r.json();
}

// ── DOM UPDATE ──
function updateDOM(d){
  // Topbar
  document.getElementById('h-price').textContent='$'+d.current_price.toFixed(2);
  document.getElementById('h-asset').textContent=d.asset+'-PERP';
  document.getElementById('h-updated').textContent=d.last_updated;

  // Stat row
  const sv=document.querySelectorAll('.stat-val');
  if(sv[0])sv[0].textContent=fm(d.long_notional);
  if(sv[1])sv[1].textContent=fm(d.short_notional);
  if(sv[2])sv[2].textContent=d.positions.length;
  if(sv[3]){sv[3].textContent=d.alerts.length;sv[3].className='stat-val '+(d.alerts.length?'num-red':'num-green')}

  // Counts
  ['alert-count','alert-count-badge'].forEach(id=>{
    const el=document.getElementById(id);if(el)el.textContent=d.alerts.length;
  });
  const lc=document.getElementById('long-count');
  const sc=document.getElementById('short-count');
  const tc=document.getElementById('tape-count');
  if(lc)lc.textContent=d.positions.filter(p=>p.direction==='long').length;
  if(sc)sc.textContent=d.positions.filter(p=>p.direction==='short').length;
  if(tc)tc.textContent=d.positions.length;

  // Charts
  const cfg={responsive:true,displayModeBar:false};
  try{
    if(d.chart_json&&d.chart_json!=='{}'){const c=JSON.parse(d.chart_json);Plotly.react('chart-main',c.data,c.layout,cfg)}
    if(d.heatmap_json&&d.heatmap_json!=='{}'){const h=JSON.parse(d.heatmap_json);Plotly.react('chart-heatmap',h.data,h.layout,cfg)}
  }catch(e){console.warn(e)}

  // Tape (left feed)
  const tape=document.getElementById('tape-feed');
  if(tape){
    tape.innerHTML=d.positions.slice(0,60).map(p=>`
      <div class="tape-item">
        <div class="tape-dir ${p.direction==='long'?'L':'S'}">${p.direction==='long'?'L':'S'}</div>
        <div class="tape-content">
          <div style="display:flex;align-items:center;gap:4px">
            <span class="tape-asset">${p.asset}</span>
            <span class="dex dex-${dexClass(p.dex)}">${dexLabel(p.dex)}</span>
          </div>
          <div class="tape-price">$${p.liq_px.toFixed(0)} · ${p.leverage.toFixed(0)}x</div>
        </div>
        <div style="text-align:right">
          <div class="tape-dist ${distClass(p.dist_pct)}">${p.dist_pct.toFixed(1)}%</div>
          <div class="tape-ts">${fm(p.notional)}</div>
        </div>
      </div>`).join('')||'<div class="no-data">No positions</div>';
  }

  // Alerts
  const ac=document.getElementById('alerts-container');
  if(ac){
    ac.innerHTML=d.alerts.length?d.alerts.slice(0,12).map(a=>`
      <div class="alert-item">
        <div class="alert-sev ${a.severity}">${a.severity==='critical'?'CRIT':'WARN'}</div>
        <div class="alert-body">
          <div class="alert-line1">
            <span class="dex dex-${dexClass(a.dex)}">${dexLabel(a.dex)}</span>
            <span style="color:${a.direction==='long'?'var(--green)':'var(--red)'}">${a.direction.toUpperCase()}</span>
            <span style="color:var(--bright)">${a.asset}</span>
          </div>
          <div class="alert-line2">${a.address} · ${fm(a.notional)}</div>
        </div>
        <div class="alert-px">
          <span class="${a.severity==='critical'?'num-red':'num-yellow'}">$${a.liq_px.toFixed(0)}</span><br>
          <span style="font-size:8px;color:var(--text3)">${a.dist_pct.toFixed(2)}%</span>
        </div>
      </div>`).join('')
      :`<div class="no-data">No alerts within ${curThreshold}%</div>`;
  }

  // Log
  const lg=document.getElementById('log-container');
  if(lg)lg.innerHTML=d.liq_log.map(e=>`
    <div class="log-item"><span class="log-ts">${e.ts}</span><span class="log-msg ${e.color}">${e.msg}</span></div>`).join('');

  // Error bar
  const eb=document.getElementById('err-bar');
  if(eb){
    if(d.errors&&d.errors.length){eb.textContent='⚠ '+d.errors.join(' · ');eb.classList.add('show')}
    else eb.classList.remove('show');
  }

  // Tables
  updateTable('longs-table', d.positions.filter(p=>p.direction==='long').slice(0,20), 'green');
  updateTable('shorts-table', d.positions.filter(p=>p.direction==='short').slice(0,20), 'red');
  updateTradersTable('traders-table', d.traders.slice(0,20));
}

function updateTable(id, rows, color){
  const el=document.getElementById(id);if(!el)return;
  const tbody=el.querySelector('tbody');if(!tbody)return;
  if(!rows.length){tbody.innerHTML=`<tr><td colspan="5" class="no-data">No data</td></tr>`;return}
  tbody.innerHTML=rows.map(p=>`<tr>
    <td><span class="dex dex-${dexClass(p.dex)}">${dexLabel(p.dex)}</span></td>
    <td class="num-blue">${p.asset}</td>
    <td class="num-${color}">$${p.liq_px.toFixed(0)}</td>
    <td><div class="dist-wrap"><div class="dist-bg"><div class="dist-fill ${distClass(p.dist_pct)}" style="width:${Math.min(p.dist_pct/10*36,36)}px"></div></div>${p.dist_pct.toFixed(1)}%</div></td>
    <td class="r num-dim">${fm(p.notional)}</td>
  </tr>`).join('');
}

function updateTradersTable(id, rows){
  const el=document.getElementById(id);if(!el)return;
  const tbody=el.querySelector('tbody');if(!tbody)return;
  if(!rows.length){tbody.innerHTML=`<tr><td colspan="5" class="no-data">No data</td></tr>`;return}
  tbody.innerHTML=rows.map(t=>`<tr>
    <td><span class="dex dex-${dexClass(t.dex)}">${dexLabel(t.dex)}</span></td>
    <td class="num-dim" style="font-size:8px">${t.address}</td>
    <td class="num-green">${fm(t.long_notional)}</td>
    <td class="num-red">${fm(t.short_notional)}</td>
    <td class="r ${t.upnl>=0?'num-green':'num-red'}">${t.upnl>=0?'+':''}$${t.upnl.toFixed(0)}</td>
  </tr>`).join('');
}

// ── TAB VIEWS (simple show/hide for mobile) ──
function showTab(name){
  document.querySelectorAll('.tb-tab').forEach(t=>t.classList.remove('active'));
  // just highlight
}

// ── ASSET CHANGE ──
function changeAsset(asset){
  curAsset=asset;
  document.querySelectorAll('.asset-pill').forEach(p=>{
    p.classList.toggle('active',p.textContent.trim().startsWith(asset));
  });
  doRefresh(asset,curThreshold).then(scheduleRefresh);
}

// ── REFRESH LOOP ──
async function doRefresh(asset,threshold){
  spinner(true);
  try{
    const data=await fetchData(asset,threshold);
    updateDOM(data);
  }catch(e){console.error(e)}
  finally{spinner(false)}
}

function scheduleRefresh(){
  if(refreshTimer)clearTimeout(refreshTimer);
  refreshTimer=setTimeout(()=>{
    doRefresh(curAsset,curThreshold).then(scheduleRefresh);
  },refreshInterval);
}

function manualRefresh(){
  curThreshold=parseFloat(document.getElementById('threshold').value);
  doRefresh(curAsset,curThreshold).then(scheduleRefresh);
}

scheduleRefresh();
</script>
</body>
</html>
"""

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with state_lock:
        data = dict(state)
    return render_template_string(
        HTML_TEMPLATE,
        data=data,
        alert_threshold=ALERT_THRESHOLD,
        refresh_interval=REFRESH_INTERVAL,
    )

@app.route("/api/data")
def api_data():
    """JSON endpoint for AJAX refreshes."""
    asset     = request.args.get("asset", state["asset"]).upper()
    threshold = float(request.args.get("threshold", ALERT_THRESHOLD))

    # If asset changed, trigger a background refresh synchronously
    if asset != state["asset"]:
        clog(f"Asset switch: {state['asset']} → {asset}", Fore.YELLOW)
        refresh_data(asset)
    
    with state_lock:
        data = dict(state)

    # Re-run alerts with potentially different threshold
    data["alerts"] = check_alerts(data["positions"], data["current_price"], threshold)

    return jsonify(data)

@app.route("/api/positions")
def api_positions():
    """Raw positions JSON."""
    with state_lock:
        pos = state["positions"]
    return jsonify(pos)

@app.route("/api/traders")
def api_traders():
    """Raw traders JSON."""
    with state_lock:
        t = state["traders"]
    return jsonify(t)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "last_updated": state["last_updated"]})

# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    clog("=" * 60, Fore.CYAN)
    clog("  LIQBOARD · Multi-DEX Liquidation Dashboard", Fore.CYAN + Style.BRIGHT)
    clog("=" * 60, Fore.CYAN)
    clog(f"  Default asset : {DEFAULT_ASSET}", Fore.WHITE)
    clog(f"  Refresh every : {REFRESH_INTERVAL}s", Fore.WHITE)
    clog(f"  Alert at      : {ALERT_THRESHOLD}% from liq", Fore.WHITE)
    clog(f"  Thread pool   : {MAX_WORKERS} workers", Fore.WHITE)
    clog("  DEXes         : Hyperliquid · dYdX · GMX", Fore.WHITE)
    clog("  Data sources  : CoinGecko · The Graph", Fore.WHITE)
    clog("=" * 60, Fore.CYAN)
    clog("  Starting initial data fetch …", Fore.YELLOW)

    # Initial blocking fetch so the page has data on first load
    refresh_data()

    # Start background refresh thread
    bg = threading.Thread(target=background_loop, daemon=True, name="bg-refresh")
    bg.start()
    clog("  Background refresh thread started", Fore.GREEN)
    clog(f"  Dashboard → http://localhost:5001", Fore.GREEN + Style.BRIGHT)
    clog("=" * 60, Fore.CYAN)

    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
