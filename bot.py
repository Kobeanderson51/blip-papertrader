#!/usr/bin/env python3
"""Paper-trading bot for Solana tokens.

Trades FAKE money against LIVE DexScreener prices, charging realistic swap fees,
network fees and slippage, so you can see whether a strategy actually makes money
after costs before risking anything.

Commands:
  python bot.py once        one price check + strategy step
  python bot.py run         loop forever (poll_seconds from config.json)
  python bot.py report      balance, open positions, P&L, win rate
  python bot.py stop        EMERGENCY STOP: halts all trading, keeps logs
  python bot.py resume      clears the stop (asks you to type RESUME)
  python bot.py reset       start the paper account over (asks you to type RESET)
  python bot.py simulate N  offline self-test on N synthetic price ticks

Standard library only. No keys, no wallets, no network writes.
"""
import json, os, sys, time, random, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(DATA, "state.json")
TRADES = os.path.join(DATA, "trades.jsonl")
EVENTS = os.path.join(DATA, "events.jsonl")
STOPFILE = os.path.join(DATA, "EMERGENCY_STOP")
HISTORY_LEN = 200


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def append(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def log_event(kind, **kw):
    append(EVENTS, {"t": now(), "event": kind, **kw})


def new_state(cfg):
    return {"cash_usd": cfg["starting_balance_usd"], "start_usd": cfg["starting_balance_usd"],
            "positions": {}, "history": {}, "created": now()}


# ---------------------------------------------------------------- prices
def fetch_quotes(watchlist):
    """Return {symbol: {"price": float, "liquidity": float}} from DexScreener (free, no key)."""
    mints = ",".join(w["mint"] for w in watchlist)
    req = urllib.request.Request(f"https://api.dexscreener.com/tokens/v1/solana/{mints}",
                                 headers={"User-Agent": "blip-papertrader/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        pairs = json.load(r)
    best = {}
    for p in pairs:
        mint = p["baseToken"]["address"]
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if p.get("priceUsd") and (mint not in best or liq > best[mint]["liquidity"]):
            best[mint] = {"price": float(p["priceUsd"]), "liquidity": float(liq)}
    return {w["symbol"]: best[w["mint"]] for w in watchlist if w["mint"] in best}


# ---------------------------------------------------------------- costs
def slippage_pct(trade_usd, liquidity_usd, c):
    impact = 100 * c["slippage_liquidity_factor"] * trade_usd / max(liquidity_usd, 1)
    return max(c["min_slippage_pct"], impact)


def fill(side, price, trade_usd, liquidity, c):
    """Price you'd actually get, after slippage, plus fees in USD."""
    slip = slippage_pct(trade_usd, liquidity, c) / 100
    px = price * (1 + slip) if side == "buy" else price * (1 - slip)
    fees = trade_usd * c["swap_fee_pct"] / 100 + c["network_fee_usd"]
    return px, fees


# ---------------------------------------------------------------- strategy
def ema(values, n):
    k = 2 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def decide(sym, hist, pos, s, quote):
    """Return 'buy', 'sell:<reason>' or None. ema_momentum: buy fast-over-slow cross, exit on TP/SL/time."""
    price = quote["price"]
    if pos:
        chg = 100 * (price / pos["entry_price"] - 1)
        held = (time.time() - pos["opened_ts"]) / 60
        if chg >= s["take_profit_pct"]:
            return "sell:take_profit"
        if chg <= -s["stop_loss_pct"]:
            return "sell:stop_loss"
        if held >= s["max_hold_minutes"]:
            return "sell:time_limit"
        return None
    if len(hist) < s["slow"] + 1 or quote["liquidity"] < s["min_liquidity_usd"]:
        return None
    prev = ema(hist[:-1][-s["slow"]:], s["fast"]) > ema(hist[:-1][-s["slow"]:], s["slow"])
    cur = ema(hist[-s["slow"]:], s["fast"]) > ema(hist[-s["slow"]:], s["slow"])
    return "buy" if cur and not prev else None


# ---------------------------------------------------------------- broker
class PaperBroker:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state

    def buy(self, sym, quote):
        st, c = self.state, self.cfg["costs"]
        budget = st["cash_usd"] * self.cfg["strategy"]["position_fraction"]
        if budget < 0.5:
            return
        px, fees = fill("buy", quote["price"], budget, quote["liquidity"], c)
        qty = (budget - fees) / px
        st["cash_usd"] -= budget
        st["positions"][sym] = {"qty": qty, "entry_price": px, "cost_usd": budget,
                                "opened_ts": time.time(), "opened": now()}
        append(TRADES, {"t": now(), "mode": "paper", "side": "buy", "symbol": sym, "usd": round(budget, 4),
                        "market_price": quote["price"], "fill_price": px, "fees_usd": round(fees, 4)})

    def sell(self, sym, quote, reason):
        st, c = self.state, self.cfg["costs"]
        pos = st["positions"].pop(sym)
        gross = pos["qty"] * quote["price"]
        px, fees = fill("sell", quote["price"], gross, quote["liquidity"], c)
        proceeds = pos["qty"] * px - fees
        st["cash_usd"] += proceeds
        pnl = proceeds - pos["cost_usd"]
        append(TRADES, {"t": now(), "mode": "paper", "side": "sell", "symbol": sym, "reason": reason,
                        "usd": round(proceeds, 4), "market_price": quote["price"], "fill_price": px,
                        "fees_usd": round(fees, 4), "pnl_usd": round(pnl, 4)})


class LiveBroker:
    """Real-money mode. LOCKED. It refuses to run until every guard passes, and even then
    swap execution is intentionally not wired up yet: it gets connected (Jupiter swap API,
    a separate burner wallet holding at most max_capital_usd) only after the paper record
    earns it and you approve it."""

    def __init__(self, cfg, state):
        problems = live_guard_problems(cfg)
        if problems:
            raise SystemExit("Live mode refused:\n - " + "\n - ".join(problems))
        raise SystemExit("Live execution isn't connected yet. Paper results first, then we wire it with your approval.")


def paper_record():
    sells = [t for t in read_trades() if t["side"] == "sell" and t["mode"] == "paper"]
    return len(sells), sum(t["pnl_usd"] for t in sells)


def live_guard_problems(cfg):
    lv, p = cfg["live"], []
    if not lv["enabled"]:
        p.append('config.json live.enabled is false')
    if os.environ.get("LIVE_TRADING") != "I_ACCEPT_LOSING_IT":
        p.append('environment variable LIVE_TRADING is not set to I_ACCEPT_LOSING_IT')
    if lv["max_capital_usd"] > 10 or lv["max_trade_usd"] > lv["max_capital_usd"]:
        p.append("caps exceed $10 capital / per-trade limit")
    n, pnl = paper_record()
    if n < lv["require_paper_trades"]:
        p.append(f"only {n} closed paper trades (need {lv['require_paper_trades']})")
    if lv["require_paper_net_positive"] and pnl <= 0:
        p.append(f"paper P&L after costs is ${pnl:.2f} (must be positive)")
    if os.path.exists(STOPFILE):
        p.append("EMERGENCY STOP is active")
    return p


# ---------------------------------------------------------------- engine
def step(cfg, state, quotes):
    if os.path.exists(STOPFILE) or not cfg["enabled"]:
        return "stopped"
    broker = PaperBroker(cfg, state) if cfg["mode"] == "paper" else LiveBroker(cfg, state)
    for sym, q in quotes.items():
        hist = state["history"].setdefault(sym, [])
        hist.append(q["price"])
        del hist[:-HISTORY_LEN]
        action = decide(sym, hist, state["positions"].get(sym), cfg["strategy"], q)
        if action == "buy":
            broker.buy(sym, q)
        elif action and action.startswith("sell"):
            broker.sell(sym, q, action.split(":")[1])
    state["last_quotes"] = quotes
    state["last_tick"] = now()
    return "ok"


def read_trades():
    try:
        with open(TRADES) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def report(cfg, state):
    q = state.get("last_quotes", {})
    open_val = sum(p["qty"] * q.get(s, {}).get("price", p["entry_price"]) for s, p in state["positions"].items())
    equity = state["cash_usd"] + open_val
    sells = [t for t in read_trades() if t["side"] == "sell"]
    fees = sum(t["fees_usd"] for t in read_trades())
    wins = sum(1 for t in sells if t["pnl_usd"] > 0)
    lines = [
        "PAPER TRADER" + ("  [EMERGENCY STOP ACTIVE]" if os.path.exists(STOPFILE) else ""),
        f"Mode: {cfg['mode']}   Live trading: {'UNLOCKED' if not live_guard_problems(cfg) else 'locked'}",
        f"Started with: ${state['start_usd']:.2f}   Now worth: ${equity:.2f}   "
        f"Change: {equity - state['start_usd']:+.2f} ({100 * (equity / state['start_usd'] - 1):+.1f}%)",
        f"Cash: ${state['cash_usd']:.2f}   Open positions: {len(state['positions'])}",
        f"Closed trades: {len(sells)}   Wins: {wins}   Win rate: {100 * wins / len(sells):.0f}%" if sells else "Closed trades: 0",
        f"Total fees + network paid: ${fees:.2f}",
        f"Last price check: {state.get('last_tick', 'never')}",
    ]
    for s, p in state["positions"].items():
        cur = q.get(s, {}).get("price", p["entry_price"])
        lines.append(f"  {s}: entry {p['entry_price']:.6g} now {cur:.6g} ({100 * (cur / p['entry_price'] - 1):+.2f}%)")
    return "\n".join(lines)


def main(argv):
    global TRADES
    os.makedirs(DATA, exist_ok=True)
    cfg = load_json(CONFIG, None)
    state = load_json(STATE, None) or new_state(cfg)
    cmd = argv[1] if len(argv) > 1 else "report"

    if cmd == "stop":
        open(STOPFILE, "w").write(now())
        log_event("emergency_stop")
        print("EMERGENCY STOP active. Nothing will trade. Logs kept in data/.\n")
        print(report(cfg, state))
    elif cmd == "resume":
        if input("Type RESUME to restart trading: ").strip() == "RESUME":
            if os.path.exists(STOPFILE):
                os.remove(STOPFILE)
            log_event("resume")
            print("Resumed.")
    elif cmd == "reset":
        if input("Type RESET to wipe the paper account: ").strip() == "RESET":
            for p in (STATE, TRADES):
                if os.path.exists(p):
                    os.rename(p, p + f".{int(time.time())}.bak")
            log_event("reset")
            print("Paper account reset (old logs kept as .bak).")
    elif cmd == "report":
        print(report(cfg, state))
    elif cmd in ("once", "run"):
        while True:
            try:
                status = step(cfg, state, fetch_quotes(cfg["watchlist"]))
                save_json(STATE, state)
                print(f"[{now()}] {status}  equity report:\n{report(cfg, state)}\n")
            except SystemExit:
                raise
            except Exception as e:  # network hiccups shouldn't kill the loop
                log_event("error", error=repr(e))
                print(f"[{now()}] error: {e!r}")
            if cmd == "once":
                break
            time.sleep(cfg["poll_seconds"])
            cfg = load_json(CONFIG, cfg)
    elif cmd == "simulate":
        n = int(argv[2]) if len(argv) > 2 else 2000
        random.seed(7)
        sim = new_state(cfg)
        TRADES = os.path.join(DATA, "sim_trades.jsonl")
        if os.path.exists(TRADES):
            os.remove(TRADES)
        prices = {w["symbol"]: 1.0 for w in cfg["watchlist"]}
        t0 = time.time
        clock = [t0()]
        time.time = lambda: clock[0]
        for _ in range(n):
            clock[0] += 60
            for s in prices:
                prices[s] *= 1 + random.gauss(0, 0.006)
            step(cfg, sim, {s: {"price": p, "liquidity": 1e6} for s, p in prices.items()})
        time.time = t0
        print(f"Synthetic random-walk test, {n} ticks (no real edge exists in random data):")
        print(report(cfg, sim))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
