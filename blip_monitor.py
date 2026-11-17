#!/usr/bin/env python3
"""$BLIP monitor: READ-ONLY. Watches the coin and writes data/blip.json for the dashboard.

Never trades, never signs, never touches a wallet. Sources (all free, no keys):
  DexScreener (price, market cap, volume), GeckoTerminal (candles),
  Solana public RPC (holders, bonding-curve balance).
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "blip.json")
STATE = os.path.join(DATA, "blip_state.json")
RPC = "https://api.mainnet-beta.solana.com"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOTAL_SUPPLY = 1_000_000_000
CURVE_FOR_SALE = 793_100_000  # tokens sold on a Pump.fun bonding curve before graduation

# Alert thresholds
PRICE_MOVE_PCT = 20       # price move between checks
WHALE_PCT_SUPPLY = 1.0    # one wallet buys/sells >= 1% of supply
HOLDER_JUMP = 10          # holder count changes by this many between checks


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "blip-monitor/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def rpc(method, params, tries=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(tries):
        req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)
            if "result" in out:
                return out["result"]
        except Exception:
            pass
        time.sleep(5 * (i + 1))  # public RPC rate-limits; back off
    raise RuntimeError(f"RPC {method} failed")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(path + ".tmp", path)


def main():
    os.makedirs(DATA, exist_ok=True)
    cfg = json.load(open(os.path.join(HERE, "config.json")))["blip"]
    mint, pool, creator = cfg["mint"], cfg["pool"], (cfg.get("creator_wallet") or "").strip()
    st = load(STATE, {"holders": {}, "history": [], "alerts": []})
    prev_holders, alerts = st["holders"], st["alerts"]
    out = {"updated": now(), "mint": mint, "pool": pool, "creator_wallet": creator or None, "errors": []}

    def alert(kind, msg, level="info"):
        alerts.insert(0, {"t": now(), "kind": kind, "level": level, "msg": msg})

    # --- market data
    market = {}
    try:
        pairs = get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
        if pairs:
            p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
            market = {"dex": p.get("dexId"), "price": float(p.get("priceUsd") or 0),
                      "market_cap": p.get("marketCap") or p.get("fdv"), "liquidity": (p.get("liquidity") or {}).get("usd"),
                      "volume": p.get("volume"), "change": p.get("priceChange"), "txns": p.get("txns")}
    except Exception as e:
        out["errors"].append(f"dexscreener: {e!r}")
    out["market"] = market

    try:
        rows = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
                   f"?aggregate=5&limit=288&currency=usd&token=base")["data"]["attributes"]["ohlcv_list"]
        out["candles_5m"] = sorted([[int(r[0])] + [float(x) for x in r[1:6]] for r in rows])
    except Exception as e:
        out["candles_5m"] = []
        out["errors"].append(f"geckoterminal: {e!r}")

    # --- holders (every token account for this mint)
    holders = {}
    try:
        accts = rpc("getProgramAccounts", [TOKEN_2022, {"encoding": "jsonParsed",
                    "filters": [{"memcmp": {"offset": 0, "bytes": mint}}]}])
        for a in accts:
            info = a["account"]["data"]["parsed"]["info"]
            amt = float(info["tokenAmount"]["uiAmount"] or 0)
            if amt > 0:
                holders[info["owner"]] = holders.get(info["owner"], 0) + amt
    except Exception as e:
        out["errors"].append(f"holders: {e!r}")
        holders = prev_holders  # keep last known

    curve_bal = holders.get(pool, 0)
    sold = max(0, TOTAL_SUPPLY - curve_bal) if curve_bal else None
    progress = None if sold is None else round(min(100, 100 * sold / CURVE_FOR_SALE), 2)
    wallets = {o: a for o, a in holders.items() if o != pool}

    def label(o):
        return "bonding curve" if o == pool else "you (creator)" if o == creator else ""

    top = sorted(holders.items(), key=lambda x: -x[1])[:15]
    out["holders"] = {"count": len(wallets),
                      "top": [{"owner": o, "amount": round(a), "pct": round(100 * a / TOTAL_SUPPLY, 3), "label": label(o)} for o, a in top]}
    out["bonding_curve"] = {"progress_pct": progress, "tokens_sold": sold, "graduated": market.get("dex") not in (None, "pumpfun")}
    if creator:
        out["creator_balance"] = round(holders.get(creator, 0))

    # --- alerts vs. previous check
    last = st["history"][-1] if st["history"] else None
    price = market.get("price")
    if last and price and last.get("price"):
        move = 100 * (price / last["price"] - 1)
        if abs(move) >= PRICE_MOVE_PCT:
            alert("price", f"Price {'up' if move > 0 else 'down'} {move:+.1f}% since last check", "warn")
    if prev_holders and holders is not prev_holders:
        for o in set(prev_holders) | set(holders):
            if o == pool:
                continue
            d = holders.get(o, 0) - prev_holders.get(o, 0)
            pct = 100 * abs(d) / TOTAL_SUPPLY
            if pct >= WHALE_PCT_SUPPLY:
                who = "Creator wallet" if o == creator else f"Wallet {o[:4]}…{o[-4:]}"
                alert("whale", f"{who} {'bought' if d > 0 else 'sold'} {pct:.2f}% of supply", "warn" if d < 0 else "info")
        dh = len(wallets) - len([o for o in prev_holders if o != pool])
        if abs(dh) >= HOLDER_JUMP:
            alert("holders", f"Holders {'+' if dh > 0 else ''}{dh} since last check (now {len(wallets)})")
    if last and progress is not None and last.get("progress") is not None:
        for mark in (25, 50, 75, 90):
            if last["progress"] < mark <= progress:
                alert("curve", f"Bonding curve passed {mark}%")
    if out["bonding_curve"]["graduated"] and last and not last.get("graduated"):
        alert("curve", "BLIP graduated to PumpSwap", "warn")
    if not st["history"]:
        alert("info", "Monitoring started")

    st["history"].append({"t": out["updated"], "price": price, "mcap": market.get("market_cap"),
                          "holders": len(wallets), "progress": progress, "graduated": out["bonding_curve"]["graduated"]})
    st["history"] = st["history"][-3000:]
    st["holders"], st["alerts"] = holders, alerts[:100]
    out["alerts"] = st["alerts"][:30]
    out["history"] = st["history"][-700:]
    save(STATE, st)
    save(OUT, out)
    print(f"BLIP price={price} mcap={market.get('market_cap')} holders={len(wallets)} curve={progress}% "
          f"alerts={len(alerts)} errors={out['errors']}")


if __name__ == "__main__":
    main()
