"""Scout's coin scanner: runs every bot tick (~15 min) and keeps the watchlist fresh.

Finds candidates from GeckoTerminal's trending + top Solana pools (free, no key), checks each on
DexScreener, and adds a coin only if it passes the SAME safety filters used to pick the original 27:
  - liquidity >= $200k, 24h volume >= $100k, oldest pool >= 30 days (no fresh pump-and-dumps)
  - quoted in SOL/USDC/USDT, not a stablecoin / major / staked / wrapped / tokenized stock
Coins it added are dropped again after failing the filters on 3 separate days (never while held).
The 27 base coins in config.json are never dropped automatically. Hard cap: max_universe coins.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE = os.path.join(HERE, "data", "universe.json")
EXCLUDE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USDS", "PYUSD", "USD1", "USDG", "FDUSD", "EURC", "JUP", "RAY", "JTO", "PYTH",
                   "ORCA", "DRIFT", "KMNO", "MSOL", "JITOSOL", "BSOL", "JUPSOL", "INF", "HSOL", "BNSOL", "CBBTC", "WBTC", "BTC",
                   "ETH", "WETH", "BNB", "LINK", "RENDER", "HNT", "W", "TNSR", "ZEUS", "CLOUD", "MET", "SYRUP", "ONDO", "XRP",
                   "SUI", "TRX", "JLP", "PUMP", "ZBCN", "ZEC", "HYPE", "CASH", "BLIP"}
EXCLUDE_NAME_WORDS = ("xstock", " stock", "securities", "inc.", "corp", "interactive", "wrapped", "staked", "bridged", "usd", "bitcoin", "ether", "treasury", "tokenized")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dipwatch/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def load():
    try:
        return json.load(open(UNIVERSE))
    except FileNotFoundError:
        return {"added": {}, "rejected_recent": {}, "last_prune_day": None}


def candidates():
    """Base-token mints from trending + top-volume Solana pools."""
    mints = {}
    for url in ("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1&include=base_token",
                "https://api.geckoterminal.com/api/v2/networks/solana/pools?page=1&sort=h24_volume_usd_desc&include=base_token"):
        try:
            r = get(url)
            toks = {t["id"]: t["attributes"] for t in r.get("included", [])}
            for p in r.get("data", []):
                t = toks.get(p["relationships"]["base_token"]["data"]["id"])
                if t:
                    mints[t["address"]] = {"symbol": (t.get("symbol") or "").upper().lstrip("$"), "name": t.get("name") or ""}
        except Exception:
            pass
        time.sleep(2.5)
    return mints


def check(mints):
    """DexScreener stats for up to 30 mints per call -> {mint: info or None}."""
    out = {}
    ml = list(mints)
    for i in range(0, len(ml), 30):
        try:
            pairs = get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(ml[i:i + 30]))
        except Exception:
            continue
        by = {}
        for p in pairs:
            by.setdefault(p["baseToken"]["address"], []).append(p)
        for m, ps in by.items():
            ps = [p for p in ps if (p.get("quoteToken") or {}).get("symbol") in ("SOL", "WSOL", "USDC", "USDT")]
            if not ps:
                continue
            best = max(ps, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
            out[m] = {"pool": best["pairAddress"], "liq": (best.get("liquidity") or {}).get("usd") or 0,
                      "vol24": sum((x.get("volume") or {}).get("h24") or 0 for x in ps),
                      "age_days": (time.time() * 1000 - min(x.get("pairCreatedAt") or time.time() * 1000 for x in ps)) / 864e5,
                      "mcap": best.get("marketCap") or best.get("fdv") or 0,
                      "symbol": best["baseToken"]["symbol"].upper().lstrip("$"), "name": best["baseToken"].get("name") or ""}
    return out


def passes(info, R):
    why = []
    if info["liq"] < R["min_liquidity_usd_new"]:
        why.append(f"liquidity ${info['liq'] / 1e3:.0f}k")
    if info["vol24"] < R["min_volume_usd_new"]:
        why.append(f"volume ${info['vol24'] / 1e3:.0f}k")
    if info["age_days"] < R["min_age_days_new"]:
        why.append(f"only {info['age_days']:.0f} days old")
    if info["mcap"] < R["min_mcap_usd_new"]:
        why.append(f"market cap ${info['mcap'] / 1e6:.2f}M")
    elif info["vol24"] > 5 * info["mcap"]:
        why.append("volume far above market cap (likely wash trading or bad data)")
    if info["symbol"] in EXCLUDE_SYMBOLS or any(w in info["name"].lower() for w in EXCLUDE_NAME_WORDS):
        why.append("not a meme coin")
    return why


def refresh(cfg, positions, log, run_id):
    """Scan, add passing coins, prune dead auto-added ones once a day. Returns the full watchlist."""
    R = cfg["desk_rules"]
    U = load()
    base = cfg["watchlist"]
    known = {w["mint"] for w in base} | set(U["added"])
    cap = R["max_universe"]
    cands = {m: v for m, v in candidates().items() if m not in known}
    if cands:
        stats = check(cands)
        for m, info in stats.items():
            if len(base) + len(U["added"]) >= cap:
                break
            why = passes(info, R)
            if why:
                U["rejected_recent"][m] = {"symbol": info["symbol"], "why": ", ".join(why), "t": now()}
                continue
            sym = info["symbol"]
            if any(w["symbol"] == sym for w in base) or any(a["symbol"] == sym for a in U["added"].values()):
                sym = f"{sym}-{m[:4]}"   # same ticker as another coin: keep both distinct
            U["added"][m] = {"symbol": sym, "mint": m, "pool": info["pool"], "added": now(),
                             "fails": [], "at_add": {k: round(info[k]) for k in ("liq", "vol24", "age_days", "mcap")}}
            log("Scout", sym, "ADDED to watchlist", f"trending/top pool; liquidity ${info['liq'] / 1e3:.0f}k, volume ${info['vol24'] / 1e3:.0f}k, "
                f"{info['age_days']:.0f} days old, mcap ${info['mcap'] / 1e6:.1f}M", "Chartist will watch it for dips", run_id)
    # keep the rejection log small
    U["rejected_recent"] = dict(sorted(U["rejected_recent"].items(), key=lambda x: x[1]["t"])[-60:])
    # once a day: re-check auto-added coins; drop after failing on 3 separate days (never while held)
    today = datetime.now(timezone.utc).date().isoformat()
    if U["added"] and U.get("last_prune_day") != today:
        U["last_prune_day"] = today
        stats = check({m: 1 for m in U["added"]})
        for m in list(U["added"]):
            a = U["added"][m]
            why = passes(stats[m], R) if m in stats else ["no market data"]
            if why and today not in a["fails"]:
                a["fails"].append(today)
            if len(a["fails"]) >= 3 and a["symbol"] not in positions:
                log("Scout", a["symbol"], "REMOVED from watchlist", "failed safety filters on 3 days: " + ", ".join(why), "none", run_id)
                del U["added"][m]
    json.dump(U, open(UNIVERSE, "w"), indent=1)
    return base + [{"symbol": a["symbol"], "mint": a["mint"], "pool": a["pool"], "auto": True} for a in U["added"].values()]
