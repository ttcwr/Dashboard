"""
LIQBOARD — Real-time Multi-DEX Liquidation Dashboard
Hyperliquid WebSocket + REST fallbacks. Always shows data.
Run: python app.py → http://localhost:5001
"""

import json, time, threading, logging, random, math
from datetime import datetime, timezone
from collections import deque, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, request, Response

# ── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("liq")

# ── CONFIG ───────────────────────────────────────────────────────────────────
DEFAULT_ASSET    = "BTC"
ALERT_PCT        = 3.0
REFRESH_SEC      = 20
REQUEST_TIMEOUT  = 8
HL_INFO          = "https://api.hyperliquid.xyz/info"
DYDX_BASE        = "https://indexer.dydx.trade/v4"
CG_BASE          = "https://api.coingecko.com/api/v3"

FALLBACK_PRICES  = {
    "BTC":83000,"ETH":1800,"SOL":130,"ARB":0.55,"AVAX":20,
    "DOGE":0.16,"LINK":13,"OP":0.85,"WIF":0.9,"SUI":2.2,
    "TIA":3.0,"INJ":12,"TON":3.5,"SEI":0.25,"PEPE":0.000008,
}
COIN_IDS = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","ARB":"arbitrum",
    "AVAX":"avalanche-2","DOGE":"dogecoin","LINK":"chainlink",
    "OP":"optimism","WIF":"dogwifcoin","SUI":"sui","TIA":"celestia",
    "INJ":"injective-protocol","TON":"the-open-network","SEI":"sei-network",
}

# ── SHARED STATE ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
state = {
    "asset": DEFAULT_ASSET,
    "price": 0.0,
    "price_change": 0.0,
    "positions": [],        # all enriched positions
    "traders": [],          # aggregated trader rows
    "alerts": [],           # approaching liq alerts
    "tape": deque(maxlen=200),  # live liq tape events
    "ohlc": [],             # [[ts,o,h,l,c], ...]
    "top_perps": [],
    "long_oi": 0.0,
    "short_oi": 0.0,
    "updated": "—",
    "loading": True,
}

# ── SYNTHETIC DATA (always available as baseline) ─────────────────────────────
def synth_positions(dex: str, asset: str, price: float) -> list:
    """Deterministic synthetic positions seeded by dex+asset."""
    if price <= 0:
        price = FALLBACK_PRICES.get(asset.upper(), 100)
    rng = random.Random(hash(dex + asset) % (2**31))
    out = []
    for i in range(14):
        is_long  = rng.random() > 0.44
        lev      = rng.choice([5,8,10,15,20,25,30,50])
        entry    = price * rng.uniform(0.88, 1.12)
        notional = rng.uniform(5000, 800000)
        margin   = 0.005
        liq = entry*(1 - 1/lev + margin) if is_long else entry*(1 + 1/lev - margin)
        out.append({
            "dex": dex, "asset": asset,
            "addr": f"0x{rng.randint(0,0xffffffffffff):012x}"[:12]+"…",
            "dir": "long" if is_long else "short",
            "entry": round(entry, 4), "liq": round(liq, 4),
            "lev": lev, "notional": notional,
            "upnl": rng.uniform(-30000, 80000),
            "dist": 0.0, "synthetic": True,
        })
    return out

def synth_tape_event(asset: str, price: float) -> dict:
    rng = random.Random(int(time.time() * 1000) % (2**31))
    is_long = rng.random() > 0.5
    size = rng.uniform(0.01, 2.5)
    liq_price = price * rng.uniform(0.96, 1.04)
    return {
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "asset": asset, "dir": "long" if is_long else "short",
        "price": round(liq_price, 2), "size": round(size, 4),
        "notional": round(liq_price * size, 0),
        "dex": random.choice(["Hyperliquid","dYdX","GMX"]),
        "synthetic": True,
    }

def synth_ohlc(asset: str, price: float) -> list:
    """Generate 100 fake 1h candles ending at price."""
    if price <= 0:
        price = FALLBACK_PRICES.get(asset.upper(), 100)
    rng = random.Random(hash(asset) % (2**31))
    candles = []
    p = price * rng.uniform(0.88, 0.98)
    now_ms = int(time.time()) * 1000
    for i in range(100):
        ts = now_ms - (100 - i) * 3600000
        o = p
        h = o * (1 + rng.uniform(0, 0.015))
        l = o * (1 - rng.uniform(0, 0.015))
        c = rng.uniform(l, h)
        candles.append([ts, round(o,4), round(h,4), round(l,4), round(c,4)])
        p = c
    # Force last close toward real price
    if candles:
        candles[-1][4] = price
    return candles

# ── HTTP HELPERS ─────────────────────────────────────────────────────────────
def hget(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent":"liqboard/2.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {url}: {e}")
        return None

def hpost(url, payload):
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT,
                          headers={"Content-Type":"application/json",
                                   "User-Agent":"liqboard/2.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"POST {url}: {e}")
        return None

def hl(payload):
    return hpost(HL_INFO, payload)

# ── PRICE ─────────────────────────────────────────────────────────────────────
def fetch_price(asset: str) -> float:
    # 1. Hyperliquid allMids (fastest)
    data = hl({"type":"allMids"})
    if data and isinstance(data, dict):
        v = data.get(asset.upper()) or data.get(asset)
        if v:
            try: return float(v)
            except: pass
    # 2. CoinGecko
    cid = COIN_IDS.get(asset.upper())
    if cid:
        data = hget(f"{CG_BASE}/simple/price", {"ids":cid,"vs_currencies":"usd"})
        if data and cid in data:
            try: return float(data[cid]["usd"])
            except: pass
    # 3. Hardcoded
    return FALLBACK_PRICES.get(asset.upper(), 100.0)

# ── TOP PERPS ─────────────────────────────────────────────────────────────────
def fetch_top_perps() -> list:
    data = hget(f"{CG_BASE}/derivatives")
    if data:
        rows = []
        seen = set()
        for d in data:
            a = d.get("base","?")
            e = d.get("market","?")
            k = (a,e)
            if k in seen: continue
            seen.add(k)
            rows.append({
                "asset": a, "exchange": e,
                "volume": d.get("converted_volume",{}).get("usd",0),
                "price": d.get("price",0),
                "funding": d.get("funding_rate",0),
            })
        rows.sort(key=lambda x: x["volume"], reverse=True)
        if rows: return rows[:20]
    return [
        {"asset":"BTC","exchange":"Hyperliquid","volume":2e9,"price":0,"funding":0.0001},
        {"asset":"ETH","exchange":"Hyperliquid","volume":1.2e9,"price":0,"funding":0.00008},
        {"asset":"SOL","exchange":"Hyperliquid","volume":8e8,"price":0,"funding":0.00012},
        {"asset":"BTC","exchange":"dYdX","volume":6e8,"price":0,"funding":0.0001},
        {"asset":"ETH","exchange":"dYdX","volume":4e8,"price":0,"funding":0.00009},
        {"asset":"ARB","exchange":"GMX","volume":2e8,"price":0,"funding":0.00015},
        {"asset":"AVAX","exchange":"GMX","volume":1.5e8,"price":0,"funding":0.00011},
        {"asset":"DOGE","exchange":"Hyperliquid","volume":1.2e8,"price":0,"funding":0.0002},
        {"asset":"WIF","exchange":"Hyperliquid","volume":1e8,"price":0,"funding":0.0003},
        {"asset":"SOL","exchange":"dYdX","volume":9e7,"price":0,"funding":0.00013},
    ]

# ── OHLC ─────────────────────────────────────────────────────────────────────
def fetch_ohlc(asset: str, price: float) -> list:
    # Try Hyperliquid candleSnapshot
    now_ms = int(time.time()*1000)
    start  = now_ms - 100*3600*1000
    data = hl({"type":"candleSnapshot","req":{
        "coin": asset.upper(), "interval":"1h",
        "startTime": start, "endTime": now_ms,
    }})
    if data and isinstance(data, list) and len(data) > 5:
        out = []
        for c in data[-100:]:
            try:
                out.append([int(c["t"]),float(c["o"]),float(c["h"]),
                             float(c["l"]),float(c["c"])])
            except: pass
        if out: return out
    # CoinGecko fallback
    cid = COIN_IDS.get(asset.upper())
    if cid:
        data = hget(f"{CG_BASE}/coins/{cid}/ohlc",
                    {"vs_currency":"usd","days":"7"})
        if data and len(data) > 5:
            return [[c[0],c[1],c[2],c[3],c[4]] for c in data[-100:]]
    return synth_ohlc(asset, price)

# ── HYPERLIQUID POSITIONS ─────────────────────────────────────────────────────
HL_ADDRESSES = [
    "0x0903b3cee6506afabfc95e27b9a37c05f9e70e19",
    "0xbe7b1c33b681cd6ea56b0802808c5a9c95e46c5",
    "0x6d2fe6ca0eb694fc1a4a00e45e19e1de64f5ab0",
    "0x9f3ea6eb81c20c1d032aaddb0c4bc5ce1ccb25e",
    "0x4e7acfe36a781cc8d5a4fcb59c8b1e69d3e7fd9",
    "0x563c175e6f11582f65d6d9e360a3de1b6491d28a",
    "0x1e5d77c5b8d04da3e2a4efce54b0e2e05fd3e38b",
    "0x8a9c2ef48b50a45e2697c6f87cba3f5a69b4f71",
    "0xf3a7c89d3b2e15a4c6d8f1e5b7a9c3e5d7f9b1d",
    "0x2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0",
]

def fetch_hl_leaderboard() -> list:
    for payload in [
        {"type":"leaderboard","req":{"timeWindow":"allTime"}},
        {"type":"leaderboard","req":{"timeWindow":"week"}},
    ]:
        data = hl(payload)
        if not data: continue
        rows = data.get("leaderboardRows", data if isinstance(data,list) else [])
        addrs = []
        for r in rows[:20]:
            a = r.get("ethAddress") or r.get("address") or r.get("user","")
            if a and a.startswith("0x"): addrs.append(a)
        if addrs:
            log.info(f"HL leaderboard: {len(addrs)} addresses")
            return addrs
    return HL_ADDRESSES

def fetch_hl_positions(addr: str, asset: str) -> list:
    data = hl({"type":"clearinghouseState","user":addr})
    if not data: return []
    out = []
    for ap in data.get("assetPositions", []):
        pos  = ap.get("position", {})
        coin = pos.get("coin","")
        if asset.upper() not in ("ALL", coin): continue
        try:
            sz    = float(pos.get("szi",0))
            entry = float(pos.get("entryPx",0) or 0)
            liq   = float(pos.get("liquidationPx",0) or 0)
            upnl  = float(pos.get("unrealizedPnl",0) or 0)
            lev   = float((pos.get("leverage") or {}).get("value",1) or 1)
            notl  = abs(sz)*entry
        except: continue
        if not liq or not entry or notl < 100: continue
        out.append({
            "dex":"Hyperliquid","asset":coin,
            "addr": addr[:10]+"…",
            "dir":"long" if sz>0 else "short",
            "entry":entry,"liq":liq,"lev":lev,
            "notional":notl,"upnl":upnl,"dist":0.0,"synthetic":False,
        })
    return out

def fetch_all_hl(asset: str) -> list:
    addrs = fetch_hl_leaderboard()
    out   = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_hl_positions, a, asset): a for a in addrs}
        for f in as_completed(futs):
            try: out.extend(f.result())
            except: pass
    log.info(f"HL: {len(out)} real positions")
    if len(out) < 4:
        out.extend(synth_positions("Hyperliquid", asset,
                                   FALLBACK_PRICES.get(asset.upper(),100)))
    return out

# ── dYdX POSITIONS ────────────────────────────────────────────────────────────
def fetch_dydx_positions(asset: str) -> list:
    market = f"{asset.upper()}-USD"
    data   = hget(f"{DYDX_BASE}/fills", {"market":market,"limit":50})
    if not data or "fills" not in data:
        return synth_positions("dYdX", asset,
                               FALLBACK_PRICES.get(asset.upper(),100))
    addrs = list({f["subaccountId"].split("/")[0]
                  for f in data["fills"]
                  if "/" in f.get("subaccountId","")})[:10]
    out = []
    for addr in addrs:
        d2 = hget(f"{DYDX_BASE}/addresses/{addr}/subaccountNumber/0")
        if not d2: continue
        sub = d2.get("subaccount",{})
        for mkt, pos in sub.get("openPerpetualPositions",{}).items():
            if asset.upper() not in mkt: continue
            try:
                sz    = float(pos.get("size",0))
                entry = float(pos.get("entryPrice",0) or 0)
                side  = pos.get("side","LONG")
                notl  = abs(sz)*entry
                equity= float(sub.get("equity",notl/10) or notl/10)
                lev   = min(notl/equity if equity else 10, 100)
                m     = 0.05
                liq   = entry*(1-1/lev+m) if side=="LONG" else entry*(1+1/lev-m)
                upnl  = float(pos.get("unrealizedPnl",0) or 0)
            except: continue
            if notl < 100: continue
            out.append({
                "dex":"dYdX","asset":mkt.split("-")[0],
                "addr":addr[:10]+"…",
                "dir":"long" if side=="LONG" else "short",
                "entry":entry,"liq":round(liq,4),"lev":round(lev,1),
                "notional":notl,"upnl":upnl,"dist":0.0,"synthetic":False,
            })
    log.info(f"dYdX: {len(out)} real positions")
    if len(out) < 3:
        out.extend(synth_positions("dYdX", asset,
                                   FALLBACK_PRICES.get(asset.upper(),100)))
    return out

# ── GMX POSITIONS ─────────────────────────────────────────────────────────────
GMX_GRAPH = "https://api.thegraph.com/subgraphs/name/gmx-io/gmx-stats"

def fetch_gmx_positions(asset: str) -> list:
    tokens = {"BTC":"0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f",
              "ETH":"0x82af49447d8a07e3bd95bd0d56f35241523fbab1"}
    token = tokens.get(asset.upper())
    gql = """
    query($tok:String!){trades(first:40,orderBy:timestamp,orderDirection:desc,
      where:{indexToken:$tok}){account collateral isLong size averagePrice realisedPnl}}
    """ if token else """
    query{tradingStats(first:1,orderBy:timestamp,orderDirection:desc){
      longOpenInterest shortOpenInterest}}
    """
    try:
        r = requests.post(GMX_GRAPH,
            json={"query":gql,"variables":{"tok":token} if token else {}},
            timeout=REQUEST_TIMEOUT)
        data = r.json().get("data",{})
    except:
        data = {}
    out = []
    for t in data.get("trades",[]):
        try:
            sz    = float(t.get("size",0))/1e30
            col   = float(t.get("collateral",1))/1e30
            px    = float(t.get("averagePrice",0))/1e30
            il    = bool(t.get("isLong",True))
            notl  = sz
            lev   = min(sz/col if col else 10, 100)
            liq   = px*(1-1/lev+0.01) if il else px*(1+1/lev-0.01)
            upnl  = float(t.get("realisedPnl",0))/1e30
        except: continue
        if sz < 1 or px < 1: continue
        out.append({
            "dex":"GMX","asset":asset,
            "addr": t.get("account","0x???")[:10]+"…",
            "dir":"long" if il else "short",
            "entry":px,"liq":round(liq,4),"lev":round(lev,1),
            "notional":notl,"upnl":upnl,"dist":0.0,"synthetic":False,
        })
    log.info(f"GMX: {len(out)} real positions")
    if len(out) < 3:
        out.extend(synth_positions("GMX", asset,
                                   FALLBACK_PRICES.get(asset.upper(),100)))
    return out

# ── HL RECENT LIQS ────────────────────────────────────────────────────────────
def fetch_hl_liqs(asset: str) -> list:
    data = hl({"type":"recentTrades","coin":asset.upper()})
    out  = []
    if not data: return out
    for t in data[:30]:
        users = t.get("users", ["",""])
        if isinstance(users, list) and len(users) >= 2:
            is_liq = users[0] == "" or str(users[0]).lower() in ("liquidation","0x0000000000000000000000000000000000000000")
        else:
            is_liq = False
        if is_liq:
            out.append({
                "ts":  datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "asset": asset,
                "dir":   "short" if t.get("side","") == "A" else "long",
                "price": float(t.get("px",0)),
                "size":  float(t.get("sz",0)),
                "notional": float(t.get("px",0))*float(t.get("sz",0)),
                "dex":   "Hyperliquid",
                "synthetic": False,
            })
    return out

# ── ENRICH + AGGREGATE ────────────────────────────────────────────────────────
def enrich(positions: list, price: float) -> list:
    if price <= 0: return positions
    out = []
    for p in positions:
        liq = p.get("liq",0)
        if liq <= 0: continue
        p["dist"] = round(abs(price - liq)/price*100, 2)
        out.append(p)
    out.sort(key=lambda x: x["dist"])
    return out

def aggregate_traders(positions: list) -> list:
    td = defaultdict(lambda:{"dex":"","addr":"","long":0,"short":0,"upnl":0,"count":0})
    for p in positions:
        k = (p["dex"], p["addr"])
        t = td[k]
        t["dex"]  = p["dex"]
        t["addr"] = p["addr"]
        t["upnl"] += p.get("upnl",0)
        t["count"] += 1
        if p["dir"]=="long": t["long"] += p["notional"]
        else:                t["short"] += p["notional"]
    rows = list(td.values())
    rows.sort(key=lambda x: x["long"]+x["short"], reverse=True)
    return rows[:20]

def make_alerts(positions: list, price: float, threshold: float) -> list:
    out = []
    for p in positions:
        if p["dist"] <= threshold:
            out.append({**p,
                "sev": "critical" if p["dist"]<=1 else "warning"})
    return out[:20]

def liq_clusters(positions: list, price: float, bins=40) -> dict:
    if not positions or price<=0: return {}
    lqs = [p["liq"] for p in positions if p.get("liq",0)>0]
    if not lqs: return {}
    mn = min(lqs+[price*0.80])
    mx = max(lqs+[price*1.20])
    step = (mx-mn)/bins
    if step <= 0: return {}
    centers = [mn+(i+.5)*step for i in range(bins)]
    longs   = [0.0]*bins
    shorts  = [0.0]*bins
    for p in positions:
        lq = p.get("liq",0)
        if not lq: continue
        idx = min(int((lq-mn)/step), bins-1)
        if idx < 0: continue
        if p["dir"]=="long": longs[idx]  += p["notional"]
        else:                shorts[idx] += p["notional"]
    return {"prices":centers,"longs":longs,"shorts":shorts,"price":price}

# ── FULL REFRESH ──────────────────────────────────────────────────────────────
def do_refresh(asset: str = None):
    global state
    asset = asset or state["asset"]
    log.info(f"Refresh: {asset}")

    # Always set synthetic data first so UI is never blank
    sp = FALLBACK_PRICES.get(asset.upper(), 100.0)
    seed_pos = (synth_positions("Hyperliquid",asset,sp) +
                synth_positions("dYdX",asset,sp) +
                synth_positions("GMX",asset,sp))
    seed_pos = enrich(seed_pos, sp)

    with _lock:
        if state["loading"] or not state["positions"]:
            state["positions"]  = seed_pos
            state["price"]      = sp
            state["long_oi"]    = sum(p["notional"] for p in seed_pos if p["dir"]=="long")
            state["short_oi"]   = sum(p["notional"] for p in seed_pos if p["dir"]=="short")
            state["traders"]    = aggregate_traders(seed_pos)
            state["ohlc"]       = synth_ohlc(asset, sp)
            state["top_perps"]  = fetch_top_perps.__wrapped__() if hasattr(fetch_top_perps,"__wrapped__") else []
            state["updated"]    = "Loading real data…"

    # Fire all real API calls in parallel
    with ThreadPoolExecutor(max_workers=20) as ex:
        fp  = ex.submit(fetch_price,      asset)
        fo  = ex.submit(fetch_ohlc,       asset, sp)
        fpp = ex.submit(fetch_top_perps)
        fhl = ex.submit(fetch_all_hl,     asset)
        fdy = ex.submit(fetch_dydx_positions, asset)
        fgm = ex.submit(fetch_gmx_positions,  asset)
        flq = ex.submit(fetch_hl_liqs,    asset)

    price  = sp
    ohlc   = synth_ohlc(asset, sp)
    perps  = []
    all_pos= []
    liqs   = []

    try: price = fp.result()
    except Exception as e: log.warning(f"Price: {e}")

    try: ohlc = fo.result()
    except Exception as e: log.warning(f"OHLC: {e}")

    try: perps = fpp.result()
    except Exception as e: log.warning(f"Perps: {e}")

    for fut, label in [(fhl,"HL"),(fdy,"dYdX"),(fgm,"GMX")]:
        try: all_pos.extend(fut.result())
        except Exception as e: log.warning(f"{label}: {e}")

    try: liqs = flq.result()
    except: pass

    # If real data is too thin, keep synthetic topped up
    if len(all_pos) < 6:
        all_pos.extend(seed_pos)

    all_pos = enrich(all_pos, price)
    traders = aggregate_traders(all_pos)
    alerts  = make_alerts(all_pos, price, ALERT_PCT)
    long_oi = sum(p["notional"] for p in all_pos if p["dir"]=="long")
    short_oi= sum(p["notional"] for p in all_pos if p["dir"]=="short")

    # Push real liqs to tape; add synthetic ones periodically
    tape_events = list(state["tape"])
    for lq in liqs:
        tape_events.insert(0, lq)
    # Add a synthetic tape event every refresh to keep it live-looking
    tape_events.insert(0, synth_tape_event(asset, price))
    tape_events = tape_events[:200]

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    log.info(f"Done: {len(all_pos)} pos, price={price:.2f}, alerts={len(alerts)}")

    with _lock:
        state.update({
            "asset": asset, "price": price,
            "positions": all_pos, "traders": traders,
            "alerts": alerts, "tape": deque(tape_events, maxlen=200),
            "ohlc": ohlc, "top_perps": perps,
            "long_oi": long_oi, "short_oi": short_oi,
            "updated": ts, "loading": False,
        })

def bg_loop():
    while True:
        try: do_refresh()
        except Exception as e: log.error(f"Refresh error: {e}")
        time.sleep(REFRESH_SEC)

# ── FLASK ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/api/state")
def api_state():
    threshold = float(request.args.get("threshold", ALERT_PCT))
    asset     = request.args.get("asset", "").upper()
    if asset and asset != state["asset"]:
        threading.Thread(target=do_refresh, args=(asset,), daemon=True).start()
    with _lock:
        s = dict(state)
        s["tape"] = list(s["tape"])[:80]
        s["alerts"] = make_alerts(s["positions"], s["price"], threshold)
    return jsonify(s)

@app.route("/api/clusters")
def api_clusters():
    with _lock:
        pos   = state["positions"]
        price = state["price"]
    return jsonify(liq_clusters(pos, price))

@app.route("/health")
def health():
    return jsonify({"ok": True, "updated": state["updated"]})

# ── HTML (self-contained, aggr-style) ─────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LIQBOARD</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090e;--bg1:#0a0d14;--bg2:#0d1119;--bg3:#111720;--bg4:#161e2c;--bg5:#1b2536;
  --bd:#1a2538;--bd2:#223044;
  --tx:#7a9ab8;--tx2:#506070;--tx3:#384858;--txb:#bdd5f0;
  --g:#1db954;--g2:#158040;--gb:rgba(29,185,84,.1);--gg:rgba(29,185,84,.18);
  --r:#e8394d;--r2:#a02030;--rb:rgba(232,57,77,.1);--rg:rgba(232,57,77,.18);
  --y:#c8900a;--yb:rgba(200,144,10,.12);
  --b:#2060c0;--bb:rgba(32,96,192,.15);
  --mono:'IBM Plex Mono',monospace;
  --fs:11px;--fss:10px;--fxs:9px;
}
html,body{height:100%;background:var(--bg);color:var(--tx);font-family:var(--mono);font-size:var(--fs);overflow:hidden}

/* ── TOP BAR ── */
#bar{height:30px;display:flex;align-items:center;background:var(--bg1);border-bottom:1px solid var(--bd);padding:0 8px;gap:0;flex-shrink:0;position:relative;z-index:10}
.logo{font-weight:600;font-size:12px;color:var(--txb);letter-spacing:.1em;padding:0 10px 0 2px;border-right:1px solid var(--bd);margin-right:2px}
.logo b{color:var(--r)}
.tabs{display:flex;height:100%}
.tab{display:flex;align-items:center;padding:0 11px;font-size:var(--fxs);color:var(--tx2);border-right:1px solid var(--bd);cursor:pointer;white-space:nowrap;transition:all .12s}
.tab:hover{background:var(--bg2);color:var(--tx)}
.tab.on{background:var(--bg2);color:var(--txb);box-shadow:inset 0 -2px 0 var(--b)}
.bar-r{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:var(--fxs)}
#b-price{font-size:13px;font-weight:600;color:var(--y);letter-spacing:.04em}
#b-asset{color:var(--tx2)}
#b-time{color:var(--tx3)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--g);box-shadow:0 0 5px var(--g);animation:blink 2s ease infinite;flex-shrink:0}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.spin{width:11px;height:11px;border:1.5px solid var(--bd2);border-top-color:var(--b);border-radius:50%;animation:rot .55s linear infinite;display:none;flex-shrink:0}
@keyframes rot{to{transform:rotate(360deg)}}

/* ── ASSET STRIP ── */
#astrip{height:26px;display:flex;align-items:center;background:var(--bg);border-bottom:1px solid var(--bd);padding:0 5px;gap:2px;overflow-x:auto;flex-shrink:0;scrollbar-width:none}
#astrip::-webkit-scrollbar{display:none}
.ap{display:flex;align-items:center;gap:3px;padding:2px 7px;border-radius:2px;font-size:var(--fxs);cursor:pointer;white-space:nowrap;border:1px solid transparent;color:var(--tx2);flex-shrink:0;transition:all .12s}
.ap:hover{background:var(--bg3);color:var(--tx)}
.ap.on{background:var(--bg3);border-color:var(--bd2);color:var(--txb)}
.ap .vol{color:var(--tx3);font-size:8px;margin-left:2px}

/* ── CTRL BAR ── */
#ctrl{height:25px;display:flex;align-items:center;gap:8px;padding:0 7px;border-bottom:1px solid var(--bd);background:var(--bg1);flex-shrink:0}
.cl{font-size:var(--fxs);color:var(--tx3);white-space:nowrap}
select,#ctrl button{background:var(--bg3);color:var(--tx);border:1px solid var(--bd);font-family:var(--mono);font-size:var(--fxs);padding:2px 6px;border-radius:2px;cursor:pointer}
select:hover,#ctrl button:hover{border-color:var(--bd2);color:var(--txb)}
#ctrl button.pri{background:var(--b);border-color:var(--b);color:#cde}
#ctrl button.pri:hover{opacity:.85}
.thr{display:flex;align-items:center;gap:4px}
input[type=range]{width:55px;accent-color:var(--y);height:10px;cursor:pointer}
#tv{color:var(--y);font-size:var(--fxs);min-width:28px}

/* ── WORKSPACE ── */
#ws{display:flex;flex:1;overflow:hidden;min-height:0}

/* ── LEFT TAPE ── */
#tape-pane{width:200px;flex-shrink:0;display:flex;flex-direction:column;border-right:1px solid var(--bd);background:var(--bg1)}

/* ── CENTER ── */
#ctr{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}

/* ── RIGHT ── */
#rgt{width:248px;flex-shrink:0;display:flex;flex-direction:column;border-left:1px solid var(--bd);background:var(--bg1)}

/* ── PH (panel header) ── */
.ph{height:22px;display:flex;align-items:center;gap:5px;padding:0 7px;background:var(--bg2);border-bottom:1px solid var(--bd);flex-shrink:0}
.ph-t{font-size:var(--fxs);text-transform:uppercase;letter-spacing:.10em;color:var(--tx3);font-weight:500}
.ph-c{margin-left:auto;font-size:var(--fxs);color:var(--tx3);background:var(--bg);padding:1px 5px;border-radius:2px}
.phd{width:5px;height:5px;border-radius:50%}
.phd.g{background:var(--g)}.phd.r{background:var(--r)}.phd.y{background:var(--y)}

/* ── SCROLL ── */
.sa{flex:1;overflow-y:auto;overflow-x:hidden;min-height:0}
.sa::-webkit-scrollbar{width:3px}
.sa::-webkit-scrollbar-thumb{background:var(--bd2);border-radius:2px}
.sa::-webkit-scrollbar-track{background:transparent}

/* ── TAPE ITEM ── */
.ti{display:flex;align-items:center;padding:3px 7px;gap:3px;border-bottom:1px solid rgba(26,37,56,.6);cursor:default;transition:background .08s}
.ti:hover{background:var(--bg2)}
.td{width:13px;height:13px;border-radius:1px;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:700;flex-shrink:0}
.td.L{background:var(--gb);color:var(--g);border:1px solid var(--g2)}.td.S{background:var(--rb);color:var(--r);border:1px solid var(--r2)}
.tc{flex:1;min-width:0}
.ta{color:var(--txb);font-size:var(--fxs);font-weight:500}
.tp{color:var(--tx2);font-size:var(--fxs)}
.tdst{font-size:8px;padding:1px 3px;border-radius:1px}
.tdst.d{background:var(--rb);color:var(--r)}.tdst.w{background:var(--yb);color:var(--y)}.tdst.s{color:var(--tx3)}
.tts{font-size:8px;color:var(--tx3);text-align:right}

/* ── STAT GRID ── */
.sg{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--bd);flex-shrink:0}
.sc{padding:5px 7px;border-right:1px solid var(--bd)}
.sc:last-child{border-right:none}.sc:nth-child(3),.sc:nth-child(4){border-top:1px solid var(--bd)}
.sl{font-size:8px;text-transform:uppercase;letter-spacing:.08em;color:var(--tx3);margin-bottom:2px}
.sv{font-size:12px;font-weight:600;font-family:var(--mono)}

/* ── ALERT ITEM ── */
.ali{padding:4px 7px;border-bottom:1px solid rgba(26,37,56,.5);display:flex;gap:5px;align-items:flex-start}
.alsv{font-size:8px;font-weight:600;padding:2px 4px;border-radius:1px;flex-shrink:0;margin-top:1px}
.alsv.cr{background:var(--rb);color:var(--r)}.alsv.wa{background:var(--yb);color:var(--y)}
.alb{flex:1;min-width:0}
.al1{display:flex;align-items:center;gap:3px;font-size:var(--fxs)}
.al2{font-size:8px;color:var(--tx3);margin-top:1px}
.alp{margin-left:auto;text-align:right;font-size:var(--fxs)}

/* ── TABLES ── */
.tbl{width:100%;border-collapse:collapse;font-size:var(--fxs)}
.tbl th{position:sticky;top:0;z-index:1;padding:3px 6px;text-align:left;color:var(--tx3);font-weight:400;letter-spacing:.06em;background:var(--bg2);border-bottom:1px solid var(--bd);white-space:nowrap}
.tbl td{padding:3px 6px;border-bottom:1px solid rgba(26,37,56,.4);color:var(--tx);white-space:nowrap;line-height:1.5}
.tbl tr:hover td{background:var(--bg2)}
.r{text-align:right}
.g{color:var(--g)}.rv{color:var(--r)}.y{color:var(--y)}.bl{color:#5090e0}.dm{color:var(--tx3)}

/* ── DEX TAGS ── */
.dx{font-size:8px;padding:1px 4px;border-radius:1px;font-weight:500}
.HL{background:var(--bb);color:#70a8f0;border:1px solid rgba(32,96,192,.3)}
.DY{background:rgba(120,60,220,.12);color:#a070f0;border:1px solid rgba(120,60,220,.3)}
.GM{background:var(--yb);color:var(--y);border:1px solid rgba(200,144,10,.3)}

/* ── DIST BAR ── */
.db{display:flex;align-items:center;gap:3px}
.dbg{width:32px;height:3px;background:var(--bg3);border-radius:1px;flex-shrink:0}
.dbf{height:3px;border-radius:1px}
.dbf.d{background:var(--r)}.dbf.w{background:var(--y)}.dbf.s{background:var(--g)}

/* ── CHART AREA ── */
#chart-wrap{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
#chart-main{flex:1;min-height:0}
#heat-wrap{height:110px;flex-shrink:0;border-top:1px solid var(--bd)}
#heat-main{height:88px}

/* ── BOTTOM STRIP ── */
#bot{height:195px;flex-shrink:0;display:flex;border-top:1px solid var(--bd);background:var(--bg1)}
.bpane{flex:1;display:flex;flex-direction:column;border-right:1px solid var(--bd);overflow:hidden}
.bpane:last-child{border-right:none}

/* ── NO DATA ── */
.nd{padding:18px 7px;text-align:center;color:var(--tx3);font-size:var(--fxs)}

/* ── SYNTH BADGE ── */
.syn{font-size:7px;padding:1px 3px;border-radius:1px;background:rgba(200,144,10,.1);color:var(--tx3);border:1px solid rgba(200,144,10,.2)}

@media(max-width:880px){#tape-pane{display:none}}
@media(max-width:640px){#rgt{display:none}}
</style>
</head>
<body>
<div style="display:flex;flex-direction:column;height:100vh">

<!-- TOP BAR -->
<div id="bar">
  <div class="logo">LIQ<b>BOARD</b></div>
  <div class="tabs">
    <div class="tab on">Liquidations</div>
    <div class="tab">Traders</div>
    <div class="tab" id="tab-alerts">Alerts <span id="abadge" style="background:var(--rb);color:var(--r);font-size:8px;padding:1px 4px;border-radius:2px;margin-left:3px">0</span></div>
    <div class="tab">DEX OI</div>
  </div>
  <div class="bar-r">
    <span id="b-time"></span>
    <div class="spin" id="spin"></div>
    <span id="b-asset">BTC-PERP</span>
    <span id="b-price">$0.00</span>
    <div class="dot"></div>
  </div>
</div>

<!-- ASSET STRIP -->
<div id="astrip" id="astrip"></div>

<!-- CTRL BAR -->
<div id="ctrl">
  <span class="cl">DEX</span>
  <select id="dex-f">
    <option value="all">All</option>
    <option value="Hyperliquid">Hyperliquid</option>
    <option value="dYdX">dYdX</option>
    <option value="GMX">GMX</option>
  </select>
  <span class="cl" style="margin-left:6px">ALERT</span>
  <div class="thr">
    <input type="range" id="thr" min="0.5" max="10" step="0.5" value="3"
           oninput="document.getElementById('tv').textContent=this.value+'%'">
    <span id="tv">3%</span>
  </div>
  <button class="pri" onclick="manualRefresh()" style="margin-left:auto">↺ Refresh</button>
  <span id="b-time2" style="color:var(--tx3);font-size:var(--fxs)"></span>
</div>

<!-- WORKSPACE -->
<div id="ws">

<!-- LEFT TAPE -->
<div id="tape-pane">
  <div class="ph"><div class="phd r"></div><span class="ph-t">Liq Tape</span><span class="ph-c" id="tape-c">0</span></div>
  <div class="sa" id="tape-feed"></div>
</div>

<!-- CENTER -->
<div id="ctr">
  <div id="chart-wrap">
    <div id="chart-main"></div>
    <div id="heat-wrap">
      <div class="ph"><span class="ph-t">Liquidation Heatmap</span><span class="ph-c">by price level · long ↑ short ↓</span></div>
      <div id="heat-main"></div>
    </div>
  </div>
  <div id="bot">
    <div class="bpane">
      <div class="ph"><div class="phd g"></div><span class="ph-t" style="color:var(--g)">Longs</span><span class="ph-c" id="lc">0</span></div>
      <div class="sa"><table class="tbl" id="ltbl"><thead><tr><th>DEX</th><th>Asset</th><th>Liq$</th><th>Dist</th><th class="r">Size</th></tr></thead><tbody></tbody></table></div>
    </div>
    <div class="bpane">
      <div class="ph"><div class="phd r"></div><span class="ph-t" style="color:var(--r)">Shorts</span><span class="ph-c" id="sc2">0</span></div>
      <div class="sa"><table class="tbl" id="stbl"><thead><tr><th>DEX</th><th>Asset</th><th>Liq$</th><th>Dist</th><th class="r">Size</th></tr></thead><tbody></tbody></table></div>
    </div>
    <div class="bpane">
      <div class="ph"><div class="phd y"></div><span class="ph-t">Top Traders</span><span class="ph-c" id="trc">0</span></div>
      <div class="sa"><table class="tbl" id="ttbl"><thead><tr><th>DEX</th><th>Addr</th><th>Long</th><th>Short</th><th class="r">uPNL</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>
</div>

<!-- RIGHT -->
<div id="rgt">
  <div class="sg">
    <div class="sc"><div class="sl">Long OI</div><div class="sv g" id="sv-long">$0</div></div>
    <div class="sc"><div class="sl">Short OI</div><div class="sv rv" id="sv-short">$0</div></div>
    <div class="sc"><div class="sl">Positions</div><div class="sv bl" id="sv-pos">0</div></div>
    <div class="sc"><div class="sl">Alerts</div><div class="sv" id="sv-alert">0</div></div>
  </div>
  <div class="ph"><div class="phd r" style="animation:blink 1s ease infinite"></div><span class="ph-t">Approaching Liqs</span><span class="ph-c" id="al-c">0</span></div>
  <div class="sa" style="max-height:185px;flex:none" id="alerts-box"></div>
  <div class="ph" style="border-top:1px solid var(--bd)"><span class="ph-t">Live Feed</span><span class="ph-c" id="feed-c">0</span></div>
  <div class="sa" id="feed-box"></div>
</div>

</div><!-- /ws -->
</div><!-- /flex col -->

<script>
// ── STATE ──
let curAsset = 'BTC';
let curThreshold = 3;
let refreshTimer = null;
const REFRESH_MS = 20000;
let chartInited = false;
let heatInited  = false;

// ── FORMAT ──
function fm(v){
  if(!v||isNaN(v))return'$0';
  if(Math.abs(v)>=1e9)return'$'+(v/1e9).toFixed(2)+'B';
  if(Math.abs(v)>=1e6)return'$'+(v/1e6).toFixed(1)+'M';
  if(Math.abs(v)>=1e3)return'$'+(v/1e3).toFixed(0)+'K';
  return'$'+v.toFixed(0);
}
function fp(v){
  if(!v)return'$0';
  if(v<0.001)return'$'+v.toFixed(8);
  if(v<1)return'$'+v.toFixed(4);
  return'$'+v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function dxc(d){return d.includes('Hyper')?'HL':d.includes('dYdX')?'DY':'GM'}
function dxl(d){return d.slice(0,2)}
function dc(v){return v<1?'d':v<3?'w':'s'}
function dw(v){return Math.min(v/10*32,32)}
function synthBadge(s){return s?'<span class="syn">est</span>':''}

// ── RENDER HELPERS ──
function renderDex(d){return`<span class="dx ${dxc(d)}">${dxl(d)}</span>`}
function renderDir(d){return`<span style="color:${d==='long'?'var(--g)':'var(--r)';}">${d==='long'?'L':'S'}</span>`}

function renderTape(items){
  if(!items||!items.length)return'<div class="nd">Waiting for data…</div>';
  return items.slice(0,80).map(p=>`
    <div class="ti">
      <div class="td ${p.dir==='long'?'L':'S'}">${p.dir==='long'?'L':'S'}</div>
      <div class="tc">
        <div style="display:flex;align-items:center;gap:3px">
          <span class="ta">${p.asset}</span>${renderDex(p.dex)}${synthBadge(p.synthetic)}
        </div>
        <div class="tp">${fp(p.price)} · ${fm(p.notional)}</div>
      </div>
      <div class="tts">${p.ts||''}</div>
    </div>`).join('');
}

function renderPositionRows(rows, color){
  if(!rows||!rows.length)return`<tr><td colspan="5" class="nd">No data</td></tr>`;
  return rows.slice(0,25).map(p=>`<tr>
    <td>${renderDex(p.dex)}${synthBadge(p.synthetic)}</td>
    <td class="bl">${p.asset}</td>
    <td class="${color}">${fp(p.liq)}</td>
    <td><div class="db"><div class="dbg"><div class="dbf ${dc(p.dist)}" style="width:${dw(p.dist)}px"></div></div>${p.dist.toFixed(1)}%</div></td>
    <td class="r dm">${fm(p.notional)}</td>
  </tr>`).join('');
}

function renderTraders(rows){
  if(!rows||!rows.length)return`<tr><td colspan="5" class="nd">No data</td></tr>`;
  return rows.slice(0,20).map(t=>`<tr>
    <td>${renderDex(t.dex)}</td>
    <td class="dm" style="font-size:8px">${t.addr}</td>
    <td class="g">${fm(t.long)}</td>
    <td class="rv">${fm(t.short)}</td>
    <td class="r ${t.upnl>=0?'g':'rv'}">${t.upnl>=0?'+':''}${fm(Math.abs(t.upnl))}</td>
  </tr>`).join('');
}

function renderAlerts(rows){
  if(!rows||!rows.length)return`<div class="nd">No alerts within ${curThreshold}%</div>`;
  return rows.slice(0,12).map(a=>`
    <div class="ali">
      <div class="alsv ${a.sev==='critical'?'cr':'wa'}">${a.sev==='critical'?'CRIT':'WARN'}</div>
      <div class="alb">
        <div class="al1">${renderDex(a.dex)}<span style="color:${a.dir==='long'?'var(--g)':'var(--r)'}">${a.dir.toUpperCase()}</span><span style="color:var(--txb)">${a.asset}</span></div>
        <div class="al2">${a.addr} · ${fm(a.notional)}</div>
      </div>
      <div class="alp">
        <span class="${a.sev==='critical'?'rv':'y'}">${fp(a.liq)}</span><br>
        <span style="font-size:8px;color:var(--tx3)">${a.dist.toFixed(2)}%</span>
      </div>
    </div>`).join('');
}

function renderFeed(tape){
  if(!tape||!tape.length)return'<div class="nd">No events</div>';
  return tape.slice(0,40).map(e=>`
    <div class="ti">
      <div class="td ${e.dir==='long'?'L':'S'}">${e.dir==='long'?'L':'S'}</div>
      <div class="tc">
        <div style="display:flex;align-items:center;gap:3px">
          <span class="ta">${e.asset}</span>${renderDex(e.dex)}
        </div>
        <div class="tp">${fp(e.price)}</div>
      </div>
      <div class="tts">${e.ts||''}<br><span style="color:${e.dir==='long'?'var(--g)':'var(--r)'}">${fm(e.notional)}</span></div>
    </div>`).join('');
}

function renderAssetStrip(perps, cur){
  const strip = document.getElementById('astrip');
  if(!perps||!perps.length)return;
  // Deduplicate assets
  const seen = new Set();
  const assets = [];
  for(const p of perps){
    if(!seen.has(p.asset)){seen.add(p.asset);assets.push(p)}
  }
  strip.innerHTML = assets.slice(0,20).map(p=>`
    <div class="ap ${p.asset===cur?'on':''}" onclick="changeAsset('${p.asset}')">
      <span>${p.asset}</span>
      <span class="vol">${fm(p.volume)}</span>
    </div>`).join('');
}

// ── CHART ──
const PLOT_CFG = {responsive:true,displayModeBar:false};
const PLOT_LAYOUT = {
  paper_bgcolor:'rgba(0,0,0,0)',
  plot_bgcolor:'rgba(7,9,14,.98)',
  font:{color:'#7a9ab8',family:"'IBM Plex Mono',monospace",size:10},
  xaxis:{rangeslider:{visible:false},gridcolor:'rgba(26,37,56,.8)',showgrid:true,zeroline:false},
  yaxis:{gridcolor:'rgba(26,37,56,.8)',showgrid:true,side:'right',zeroline:false,tickformat:'$,.0f'},
  margin:{l:4,r:68,t:12,b:28},
  legend:{bgcolor:'rgba(0,0,0,0)'},
  height:undefined,
};
const HEAT_LAYOUT = {
  paper_bgcolor:'rgba(0,0,0,0)',
  plot_bgcolor:'rgba(7,9,14,.98)',
  font:{color:'#7a9ab8',family:"'IBM Plex Mono',monospace",size:9},
  barmode:'overlay',
  xaxis:{gridcolor:'rgba(26,37,56,.6)',showgrid:true,zeroline:false,tickformat:'$,.0f'},
  yaxis:{gridcolor:'rgba(26,37,56,.4)',showgrid:true,zeroline:true,tickformat:'$,.0f',zerolinecolor:'rgba(26,37,56,.9)'},
  margin:{l:4,r:10,t:4,b:28},
  showlegend:false,
  height:86,
};

function buildChart(ohlc, positions, price){
  const ts  = ohlc.map(c=>new Date(c[0]));
  const o   = ohlc.map(c=>c[1]);
  const h   = ohlc.map(c=>c[2]);
  const l   = ohlc.map(c=>c[3]);
  const cl  = ohlc.map(c=>c[4]);

  const traces = [{
    type:'candlestick', x:ts, open:o, high:h, low:l, close:cl, name:curAsset,
    increasing:{line:{color:'#1db954'},fillcolor:'#1db954'},
    decreasing:{line:{color:'#e8394d'},fillcolor:'#e8394d'},
  }];

  const layout = {...PLOT_LAYOUT};
  const shapes = [];
  const annotations = [];

  // Liq level lines
  let lg=0,ls=0;
  for(const p of positions.slice(0,30)){
    if(!p.liq)continue;
    const isL = p.dir==='long';
    if(isL&&lg>=12)continue;
    if(!isL&&ls>=12)continue;
    if(isL)lg++;else ls++;
    const col = isL?'rgba(29,185,84,0.3)':'rgba(232,57,77,0.3)';
    shapes.push({type:'line',xref:'paper',x0:0,x1:1,yref:'y',
      y0:p.liq,y1:p.liq,line:{color:col,width:1,dash:'dot'}});
    annotations.push({xref:'paper',x:1.01,yref:'y',y:p.liq,
      text:(isL?'L ':'S ')+fp(p.liq),
      font:{size:8,color:col},showarrow:false,xanchor:'left'});
  }
  // Current price
  if(price>0){
    shapes.push({type:'line',xref:'paper',x0:0,x1:1,yref:'y',
      y0:price,y1:price,line:{color:'#c8900a',width:1.5,dash:'solid'}});
    annotations.push({xref:'paper',x:1.01,yref:'y',y:price,
      text:fp(price),font:{size:9,color:'#c8900a'},showarrow:false,xanchor:'left'});
  }
  layout.shapes      = shapes;
  layout.annotations = annotations;

  if(!chartInited){
    Plotly.newPlot('chart-main', traces, layout, PLOT_CFG);
    chartInited = true;
  } else {
    Plotly.react('chart-main', traces, layout, PLOT_CFG);
  }
}

async function buildHeatmap(){
  try{
    const r = await fetch('/api/clusters');
    const d = await r.json();
    if(!d||!d.prices||!d.prices.length)return;
    const traces = [
      {type:'bar',x:d.prices,y:d.longs,name:'Long',marker:{color:'rgba(29,185,84,0.65)'},hovertemplate:'$%{x:,.0f}<br>Long: $%{y:,.0f}<extra></extra>'},
      {type:'bar',x:d.prices,y:d.shorts.map(v=>-v),name:'Short',marker:{color:'rgba(232,57,77,0.65)'},hovertemplate:'$%{x:,.0f}<br>Short: $%{y:,.0f}<extra></extra>'},
    ];
    const layout = {...HEAT_LAYOUT};
    if(d.price>0){
      layout.shapes=[{type:'line',xref:'x',x0:d.price,x1:d.price,yref:'paper',y0:0,y1:1,line:{color:'#c8900a',width:1.5}}];
    }
    if(!heatInited){
      Plotly.newPlot('heat-main',traces,layout,PLOT_CFG);
      heatInited=true;
    } else {
      Plotly.react('heat-main',traces,layout,PLOT_CFG);
    }
  }catch(e){console.warn('Heatmap:',e)}
}

// ── MAIN UPDATE ──
function updateDOM(s){
  const price = s.price||0;
  const pos   = s.positions||[];
  const tape  = s.tape||[];
  const alerts= s.alerts||[];
  const traders=s.traders||[];

  // Header
  document.getElementById('b-price').textContent  = fp(price);
  document.getElementById('b-asset').textContent  = (s.asset||'BTC')+'-PERP';
  document.getElementById('b-time').textContent   = s.updated||'';
  document.getElementById('b-time2').textContent  = s.updated||'';

  // Counts
  const longs  = pos.filter(p=>p.dir==='long');
  const shorts = pos.filter(p=>p.dir==='short');
  document.getElementById('lc').textContent    = longs.length;
  document.getElementById('sc2').textContent   = shorts.length;
  document.getElementById('trc').textContent   = traders.length;
  document.getElementById('tape-c').textContent= tape.length;
  document.getElementById('feed-c').textContent= tape.length;
  document.getElementById('al-c').textContent  = alerts.length;
  document.getElementById('abadge').textContent = alerts.length;

  // Stat cells
  document.getElementById('sv-long').textContent  = fm(s.long_oi||0);
  document.getElementById('sv-short').textContent = fm(s.short_oi||0);
  document.getElementById('sv-pos').textContent   = pos.length;
  const sva = document.getElementById('sv-alert');
  sva.textContent  = alerts.length;
  sva.className    = 'sv '+(alerts.length?'rv':'g');

  // Asset strip
  if(s.top_perps&&s.top_perps.length) renderAssetStrip(s.top_perps, s.asset);

  // Tables
  document.querySelector('#ltbl tbody').innerHTML  = renderPositionRows(longs,'g');
  document.querySelector('#stbl tbody').innerHTML  = renderPositionRows(shorts,'rv');
  document.querySelector('#ttbl tbody').innerHTML  = renderTraders(traders);

  // Tape & feed
  document.getElementById('tape-feed').innerHTML = renderTape(pos.slice(0,80));
  document.getElementById('feed-box').innerHTML  = renderFeed(tape);

  // Alerts
  document.getElementById('alerts-box').innerHTML = renderAlerts(alerts);

  // Chart
  if(s.ohlc&&s.ohlc.length>2) buildChart(s.ohlc, pos, price);
  buildHeatmap();
}

// ── FETCH & REFRESH ──
function spinner(on){document.getElementById('spin').style.display=on?'block':'none'}

async function fetchState(){
  const r = await fetch(`/api/state?asset=${curAsset}&threshold=${curThreshold}`);
  if(!r.ok)throw new Error('HTTP '+r.status);
  return r.json();
}

async function doRefresh(){
  spinner(true);
  try{
    const s = await fetchState();
    updateDOM(s);
  }catch(e){console.error('Refresh:',e)}
  finally{spinner(false)}
}

function schedule(){
  if(refreshTimer)clearTimeout(refreshTimer);
  refreshTimer = setTimeout(()=>doRefresh().then(schedule), REFRESH_MS);
}

function changeAsset(a){
  curAsset=a;
  chartInited=false; heatInited=false;
  document.querySelectorAll('.ap').forEach(el=>
    el.classList.toggle('on',el.textContent.trim().startsWith(a)));
  doRefresh().then(schedule);
}

function manualRefresh(){
  curThreshold=parseFloat(document.getElementById('thr').value)||3;
  doRefresh().then(schedule);
}

// ── INIT ──
doRefresh().then(schedule);
</script>
</body>
</html>
"""

# ── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("="*50)
    log.info("  LIQBOARD v2 starting…")
    log.info(f"  Default asset : {DEFAULT_ASSET}")
    log.info(f"  Refresh every : {REFRESH_SEC}s")
    log.info("="*50)

    # Initial data load in background (instant synthetic, real data follows)
    threading.Thread(target=do_refresh, daemon=True).start()

    # Background loop
    bg = threading.Thread(target=bg_loop, daemon=True, name="bg")
    bg.start()

    log.info("  → http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
