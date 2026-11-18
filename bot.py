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
import desk
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
            best[mint] = {"price": float(p["priceUsd"]), "liquidity": float(liq),
                          "change_24h": (p.get("priceChange") or {}).get("h24"),
                          "volume_24h": (p.get("volume") or {}).get("h24")}
    return {w["symbol"]: best[w["mint"]] for w in watchlist if w["mint"] in best}


def fetch_candles(pool, limit=200):
    """Last `limit` closed 1-hour candles from GeckoTerminal (free, no key), oldest first."""
    url = (f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour"
           f"?aggregate=1&limit={limit + 1}&currency=usd&token=base")
    req = urllib.request.Request(url, headers={"User-Agent": "blip-papertrader/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.load(r)["data"]["attributes"]["ohlcv_list"]
    rows.sort(key=lambda x: x[0])
    return rows[:-1]  # [ts, open, high, low, close, volume], oldest first; drop the still-forming candle


CANDLE_CACHE = os.path.join(DATA, "candles_cache.json")


def attach_history(quotes, watchlist):
    """Hourly candles change once an hour, so keep them cached and only re-download a coin when a
    newer closed candle should exist. That is ~1 call per coin per hour instead of 4, which keeps
    27+ coins under GeckoTerminal's free rate limit. Failed fetches back off and fall back to cache."""
    cache = load_json(CANDLE_CACHE, {})
    nowt = time.time()
    for w in watchlist:
        sym = w["symbol"]
        if sym not in quotes or not w.get("pool"):
            continue
        rows = cache.get(sym, {}).get("rows") or []
        stale = not rows or nowt >= rows[-1][0] + 2 * 3600 + 60   # next candle has closed
        if stale:
            for wait in (0, 8, 20):
                if wait:
                    time.sleep(wait)
                try:
                    fresh = fetch_candles(w["pool"])
                    rows = [[int(r[0])] + [float(x) for x in r[1:5]] for r in fresh]
                    cache[sym] = {"rows": rows[-200:], "fetched": nowt}
                    break
                except Exception as e:
                    err = repr(e)
            else:
                log_event("candles_error", symbol=sym, error=err)
            time.sleep(2.5)  # stay under 30 calls/minute
        if rows:
            quotes[sym]["closes"] = [r[4] for r in rows]
            quotes[sym]["candles"] = rows
    save_json(CANDLE_CACHE, cache)
    return quotes


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


def rsi(closes, n=14):
    """Wilder RSI of a list of closes; returns list aligned to closes (None during warm-up)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def decide(sym, hist, pos, s, quote, state=None):
    """rsi_dip (validated in research/, Nov 2026): on CLOSED 1h candles, buy when RSI(14) crosses below
    `rsi_buy`; sell when RSI closes above `rsi_exit`, price hits the stop, or after `max_hold_hours`."""
    closes = quote.get("closes") or []
    candles = quote.get("candles") or []
    r = rsi(closes, 14)
    cur, prev = (r[-1], r[-2]) if len(r) >= 2 else (None, None)
    if pos:
        if quote["price"] <= pos["entry_price"] * (1 - s["stop_loss_pct"] / 100):
            return "sell:stop_loss"
        if cur is not None and cur > s["rsi_exit"]:
            return "sell:rsi_recovered"
        if (time.time() - pos["opened_ts"]) / 3600 >= s["max_hold_hours"]:
            return "sell:time_limit"
        return None
    if cur is None or prev is None or quote["liquidity"] < s["min_liquidity_usd"]:
        return None
    if len(state["positions"]) >= s["max_positions"]:
        return None
    last_candle = candles[-1][0] if candles else None
    sig = state.setdefault("last_signal", {})
    if cur < s["rsi_buy"] <= prev and sig.get(sym) != last_candle:   # crossed below on the last closed candle
        sig[sym] = last_candle  # one entry per candle even though we check every 15 min
        return "buy"
    return None


# ---------------------------------------------------------------- broker
JOURNAL = os.path.join(DATA, "journal.jsonl")


def post_mortem(sym, pos, quote, reason, pnl, st, cfg):
    """What happened in this trade and what would have done better. Lessons are observations,
    not rule changes: rules only change after a weekly review + backtest + the user's approval."""
    e = pos["entry_price"]
    mfe = 100 * (pos.get("peak", e) / e - 1)      # best it got while held
    mae = 100 * (pos.get("trough", e) / e - 1)    # worst it got while held
    held_h = (time.time() - pos["opened_ts"]) / 3600
    mkt = st.get("market_24h")
    r = rsi(quote.get("closes") or [], 14)
    lessons, tags = [], []
    if pnl <= 0:
        if mfe >= 3:
            lessons.append(f"Was up {mfe:.1f}% before turning into a loss. A take-profit near +3% or a trailing stop would have locked in a win.")
            tags.append("gave_back_gains")
        if mkt is not None and mkt <= -5:
            lessons.append(f"The whole basket was down {mkt:.1f}% over 24h. Buying a dip during a market-wide selloff tends to keep falling.")
            tags.append("market_selloff")
        if reason == "stop_loss" and held_h < 3:
            lessons.append(f"Hit the stop within {held_h:.1f}h. The dip kept dipping; waiting for RSI to turn back up before buying might have avoided it.")
            tags.append("falling_knife")
        if reason == "time_limit":
            lessons.append("Never recovered within 24h. Dead-cat bounce did not come; a shorter hold or a recovery check could cut these sooner.")
            tags.append("no_bounce")
        if not lessons:
            lessons.append(f"Small loss ({mae:.1f}% worst point). Fees and slippage (~1.1% round trip) ate a move that was too small.")
            tags.append("fees_ate_it")
    else:
        if mfe - 100 * (pnl / pos["cost_usd"]) > 3:
            lessons.append(f"Won, but it was up {mfe:.1f}% at best. Selling earlier would have made more.")
            tags.append("left_money")
        else:
            lessons.append("Worked as designed: bought the oversold dip and sold on the recovery.")
            tags.append("clean_win")
    return {"t": now(), "symbol": sym, "reason": reason, "pnl_usd": round(pnl, 4),
            "pnl_pct": round(100 * pnl / pos["cost_usd"], 2), "held_hours": round(held_h, 1),
            "best_pct": round(mfe, 2), "worst_pct": round(mae, 2), "rsi_entry": pos.get("rsi_entry"),
            "rsi_exit": r[-1] if r and r[-1] is not None else None, "market_24h_entry": pos.get("market_24h_entry"),
            "market_24h_exit": mkt, "tags": tags, "lessons": lessons}


class PaperBroker:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state

    def buy(self, sym, quote):
        st, c = self.state, self.cfg["costs"]
        q = st.get("last_quotes", {})
        equity = st["cash_usd"] + sum(p["qty"] * q.get(s_, {}).get("price", p["entry_price"]) for s_, p in st["positions"].items())
        budget = min(st["cash_usd"], equity / self.cfg["strategy"]["max_positions"])
        if budget < 0.5:
            return
        px, fees = fill("buy", quote["price"], budget, quote["liquidity"], c)
        qty = (budget - fees) / px
        st["cash_usd"] -= budget
        r = rsi(quote.get("closes") or [], 14)
        st["positions"][sym] = {"qty": qty, "entry_price": px, "cost_usd": budget,
                                "opened_ts": time.time(), "opened": now(), "peak": px, "trough": px,
                                "rsi_entry": r[-1] if r and r[-1] is not None else None,
                                "market_24h_entry": st.get("market_24h")}
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
        append(JOURNAL, post_mortem(sym, pos, quote, reason, pnl, st, self.cfg))


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
    if os.path.exists(STOPFILE) or not cfg["enabled"] or state.get("kill"):
        return "stopped"
    broker = PaperBroker(cfg, state) if cfg["mode"] == "paper" else LiveBroker(cfg, state)
    ch = [q.get("change_24h") for q in quotes.values() if q.get("change_24h") is not None]
    state["market_24h"] = round(sum(ch) / len(ch), 2) if ch else None
    for sym, p in state["positions"].items():
        if sym in quotes:
            p["peak"] = max(p.get("peak", p["entry_price"]), quotes[sym]["price"])
            p["trough"] = min(p.get("trough", p["entry_price"]), quotes[sym]["price"])
    run_id = now()
    rules = cfg["desk_rules"]
    for sym, q in quotes.items():
        state["history"][sym] = (q.get("closes") or [])[-HISTORY_LEN:]
    state["last_quotes"] = {k: {x: v.get(x) for x in ("price", "liquidity", "change_24h", "volume_24h")} for k, v in quotes.items()}
    # Exits first: every open position is checked against its plan (stop / RSI recovered / time limit).
    for sym in list(state["positions"]):
        if sym in quotes:
            action = decide(sym, None, state["positions"][sym], cfg["strategy"], quotes[sym], state)
            if action and action.startswith("sell"):
                broker.sell(sym, quotes[sym], action.split(":")[1])
                desk.log("Executor", sym, "SOLD (paper)", action.split(":")[1].replace("_", " "), "post-mortem in Lessons", run_id)
    equity = state["cash_usd"] + sum(p["qty"] * quotes.get(s_, {}).get("price", p["entry_price"]) for s_, p in state["positions"].items())
    closed = [t for t in read_trades() if t["side"] == "sell"]
    # New ideas: Scout -> Chartist -> Quant -> Risk Manager -> Executor. No seat is skipped.
    tickers = desk.scout(quotes, rules, run_id)
    setups = desk.chartist(tickers, quotes, rsi, cfg["strategy"], state, run_id)
    plans = desk.quant(setups, quotes, cfg, state, equity, run_id)
    approved = desk.risk_manager(plans, cfg, state, equity, closed, run_id)
    desk.executor(approved, quotes, broker, run_id)
    desk.desk_lead_brief(state, equity, closed, cfg, run_id)
    desk.trim_log()
    state["last_quotes"] = {k: {x: v.get(x) for x in ("price", "liquidity", "change_24h", "volume_24h")} for k, v in quotes.items()}
    state["candles"] = {k: v["candles"] for k, v in quotes.items() if v.get("candles")}
    state["last_tick"] = now()
    return "ok"


def read_journal():
    try:
        with open(JOURNAL) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def lesson_stats():
    j = read_journal()
    losses = [x for x in j if x["pnl_usd"] <= 0]
    counts = {}
    for x in losses:
        for t in x["tags"]:
            counts[t] = counts.get(t, 0) + 1
    return {"trades": len(j), "losses": len(losses), "loss_patterns": dict(sorted(counts.items(), key=lambda x: -x[1]))}


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
        "DIPWATCH (paper)" + ("  [EMERGENCY STOP ACTIVE]" if os.path.exists(STOPFILE) else ""),
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


def write_status(cfg, state):
    """data/status.json: everything the dashboard grid shows."""
    q = state.get("last_quotes", {})
    s_ = cfg["strategy"]
    positions = []
    for sym, p in state["positions"].items():
        cur = q.get(sym, {}).get("price", p["entry_price"])
        positions.append({"symbol": sym, "entry": p["entry_price"], "now": cur, "qty": p["qty"],
                          "cost_usd": round(p["cost_usd"], 4), "opened_ts": p["opened_ts"], "value_usd": round(p["qty"] * cur, 4),
                          "pnl_pct": round(100 * (cur / p["entry_price"] - 1), 2), "opened": p["opened"],
                          "take_profit": None,
                          "stop_loss": p["entry_price"] * (1 - s_["stop_loss_pct"] / 100)})
    equity = state["cash_usd"] + sum(p["value_usd"] for p in positions)
    watch = []
    for w in cfg["watchlist"]:
        sym = w["symbol"]
        h = state["history"].get(sym, [])
        rv = rsi(h, 14)[-1] if len(h) > 15 else None
        watch.append({"symbol": sym, "price": q.get(sym, {}).get("price"), "liquidity": q.get(sym, {}).get("liquidity"),
                      "change_24h": q.get(sym, {}).get("change_24h"), "volume_24h": q.get(sym, {}).get("volume_24h"),
                      "candles": state.get("candles", {}).get(sym, []),
                      "rsi": None if rv is None else round(rv, 1),
                      "trend": None if rv is None else ("oversold" if rv < s_["rsi_buy"] else "up" if rv >= 50 else "down"),
                      "holding": sym in state["positions"]})
    trades = read_trades()
    sells = [t for t in trades if t["side"] == "sell"]
    curve = state.setdefault("equity_curve", [])
    curve.append({"t": now(), "equity": round(equity, 4)})
    del curve[:-2000]
    check_milestones(cfg, state, equity)
    save_json(STATE, state)
    save_json(os.path.join(DATA, "status.json"), {
        "updated": now(), "mode": cfg["mode"], "stopped": os.path.exists(STOPFILE) or bool(state.get("kill")),
        "start_usd": state["start_usd"], "goal_usd": cfg.get("goal_usd"), "equity_usd": round(equity, 4),
        "cash_usd": round(state["cash_usd"], 4), "change_pct": round(100 * (equity / state["start_usd"] - 1), 2),
        "closed_trades": len(sells), "wins": sum(1 for t in sells if t["pnl_usd"] > 0),
        "realized_pnl_usd": round(sum(t["pnl_usd"] for t in sells), 4),
        "fees_usd": round(sum(t["fees_usd"] for t in trades), 4),
        "strategy": {k: s_[k] for k in ("name", "rsi_buy", "rsi_exit", "stop_loss_pct", "max_hold_hours", "max_positions")},
        "kill": state.get("kill"),
        "desk": {"rules": cfg["desk_rules"], "brief": load_json(os.path.join(DATA, "brief.json"), None),
                 "log": [json.loads(l) for l in (open(desk.LOG).read().splitlines() if os.path.exists(desk.LOG) else [])[-80:]][::-1]},
        "journal": read_journal()[-30:][::-1], "lesson_stats": lesson_stats(),
        "market_24h": state.get("market_24h"),
        "live": {"unlocked": not live_guard_problems(cfg), "problems": live_guard_problems(cfg),
                 "burner_address": (cfg["live"].get("burner_address") or None), "max_capital_usd": cfg["live"]["max_capital_usd"],
                 "max_trade_usd": cfg["live"]["max_trade_usd"], "require_paper_trades": cfg["live"]["require_paper_trades"]},
        "positions": positions, "watchlist": watch, "trades": trades[-100:][::-1], "equity_curve": curve[-500:]})


def check_milestones(cfg, state, equity):
    """Queue a one-time notification (data/notify.md) when the goal is hit or half the account is gone."""
    n = cfg.get("notify") or {}
    done = state.setdefault("notified", {})
    msgs = []
    if n.get("goal_usd") and equity >= n["goal_usd"] and not done.get("goal"):
        done["goal"] = now()
        msgs.append(("Dipwatch hit the goal: ${:.2f}".format(equity),
                     "The paper account reached ${:.2f} (goal ${:.0f}). This is simulated money.\n\n"
                     "Before any real money: review the trade log, win rate and fees on the dashboard, "
                     "and remember a good run can be luck. Live mode stays locked until you decide.".format(equity, n["goal_usd"])))
    if n.get("warn_below_usd") and equity <= n["warn_below_usd"] and not done.get("warn"):
        done["warn"] = now()
        msgs.append(("Dipwatch is down to ${:.2f}".format(equity),
                     "The paper account fell to ${:.2f} from ${:.0f}. This strategy is losing; "
                     "it should not get real money as-is.".format(equity, state["start_usd"])))
    # Paper test complete: 50 closed trades -> tell the user whether it passed.
    closed = [t for t in read_trades() if t["side"] == "sell"]
    if len(closed) >= 50 and not done.get("test_complete"):
        done["test_complete"] = now()
        pnl = sum(t["pnl_usd"] for t in closed)
        wins = sum(1 for t in closed if t["pnl_usd"] > 0)
        fees = sum(t["fees_usd"] for t in read_trades())
        by = {}
        for t in closed:
            by[t["symbol"]] = by.get(t["symbol"], 0) + t["pnl_usd"]
        best = max(by.items(), key=lambda x: x[1])
        passed = pnl > 0 and not state.get("kill")
        msgs.append(("Paper test complete: " + ("PASSED" if passed else "FAILED") + " (${:+.2f} after costs over {} trades)".format(pnl, len(closed)),
                     "Closed trades: {}\nWins: {} ({:.0f}%)\nRealized P&L after costs: ${:+.2f}\nFees paid: ${:.2f}\nAccount now: ${:.2f}\n"
                     "Best coin: {} (${:+.2f}){}\n\n{}".format(
                         len(closed), wins, 100 * wins / len(closed), pnl, fees, equity, best[0], best[1],
                         " - most of the profit came from one coin, so treat it as possible luck" if pnl > 0 and best[1] > pnl * 0.6 else "",
                         "Next step: review together, then the live $10 setup needs your explicit APPROVE LIVE."
                         if passed else "This strategy should not get real money. Paper trading continues; nothing live was turned on.")))

    # Kill rules (from research/ bear case): stop trading automatically, keep logs.
    k = cfg.get("kill_rules") or {}
    peak = state["peak_equity"] = max(state.get("peak_equity", state["start_usd"]), equity)
    sells = [t for t in read_trades() if t["side"] == "sell"]
    reason = None
    if k.get("max_drawdown_pct") and equity <= peak * (1 - k["max_drawdown_pct"] / 100):
        reason = f"account fell {k['max_drawdown_pct']}% from its high (${peak:.2f} -> ${equity:.2f})"
    elif k.get("review_after_trades") and len(sells) >= k["review_after_trades"] and sum(t["pnl_usd"] for t in sells) <= 0:
        reason = f"{len(sells)} closed trades and realized P&L after costs is ${sum(t['pnl_usd'] for t in sells):.2f}"
    if reason and not state.get("kill"):
        state["kill"] = {"t": now(), "reason": reason}
        open(STOPFILE, "w").write(now() + " kill rule: " + reason)
        msgs.append(("Dipwatch stopped by kill rule", "Trading stopped automatically: " + reason +
                     ". Open positions are left as they are and logs are kept. The strategy has failed its test; "
                     "it should not get real money."))
    if msgs:
        with open(os.path.join(DATA, "notify.md"), "w") as f:
            title, body = msgs[0]
            f.write(title + "\n" + body + "".join("\n\n---\n" + t + "\n" + b for t, b in msgs[1:]))


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
                status = step(cfg, state, attach_history(fetch_quotes(cfg["watchlist"]), cfg["watchlist"]))
                save_json(STATE, state)
                write_status(cfg, state)
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
