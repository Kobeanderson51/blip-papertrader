"""The Dipwatch trading desk: six seats, one rulebook (config.json "desk_rules").

Every idea moves Scout -> Chartist -> Quant -> Risk Manager -> Executor, and no idea skips a seat.
The Desk Lead writes the daily brief and the log. Each seat is plain code, so the rules are
walls, not requests: no model can talk its way past Risk Manager.

Every handoff is logged to data/desk_log.jsonl as  SEAT | TICKER | IDEA | EVIDENCE | NEXT STEP.
"""
import json, os, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
LOG = os.path.join(DATA, "desk_log.jsonl")
BRIEF = os.path.join(DATA, "brief.json")
LOG_KEEP = 1500


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(seat, ticker, idea, evidence, next_step, run_id):
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": now(), "run": run_id, "seat": seat, "ticker": ticker, "idea": idea,
                            "evidence": evidence, "next": next_step}) + "\n")


def trim_log():
    try:
        lines = open(LOG).read().splitlines()
    except FileNotFoundError:
        return
    if len(lines) > LOG_KEEP:
        open(LOG, "w").write("\n".join(lines[-LOG_KEEP:]) + "\n")


# ------------------------------------------------------------------ seats
def scout(quotes, rules, run_id):
    """Find what is tradable right now and pass it on. Skips thin pools and stale data."""
    out = []
    for sym, q in quotes.items():
        candles = q.get("candles") or []
        age_h = (time.time() - candles[-1][0]) / 3600 - 1 if candles else 99   # hours since last closed candle ended
        if q["liquidity"] < rules["min_liquidity_usd"]:
            log("Scout", sym, "skip", f"liquidity ${q['liquidity']:,.0f} below ${rules['min_liquidity_usd']:,}", "none", run_id)
            continue
        if age_h > rules["stale_data_hours"]:
            log("Scout", sym, "skip", f"no trades in the last {age_h:.1f}h (thin or stale)", "none", run_id)
            continue
        out.append(sym)
        ch = q.get("change_24h")
        if ch is not None and abs(ch) >= rules["mover_pct_24h"]:
            log("Scout", sym, "big mover", f"{ch:+.1f}% in 24h, liquidity ${q['liquidity']:,.0f}", "Chartist: check for a dip setup", run_id)
    return out


def chartist(tickers, quotes, rsi_fn, s, state, run_id):
    """Does the chart show the setup? rsi_dip: RSI(14) on the last CLOSED 1h candle crossed below rsi_buy."""
    setups = []
    sig = state.setdefault("last_signal", {})
    for sym in tickers:
        if sym in state["positions"]:
            continue
        q = quotes[sym]
        r = rsi_fn(q.get("closes") or [], 14)
        if len(r) < 2 or r[-1] is None or r[-2] is None:
            continue
        cur, prev = r[-1], r[-2]
        last_candle = q["candles"][-1][0] if q.get("candles") else None
        if cur < s["rsi_buy"] <= prev and sig.get(sym) != last_candle:
            sig[sym] = last_candle
            score = min(10, 7 + int((s["rsi_buy"] - cur) / 3))   # deeper oversold = higher score
            setups.append({"symbol": sym, "rsi": round(cur, 1), "score": score})
            log("Chartist", sym, "oversold dip", f"RSI {prev:.0f} -> {cur:.0f} (below {s['rsi_buy']}), score {score}/10", "Quant: write the plan", run_id)
        elif cur < s["rsi_buy"] + 10:
            log("Chartist", sym, "watch", f"RSI {cur:.0f}, needs to cross below {s['rsi_buy']}", "keep watching", run_id)
    return setups


def quant(setups, quotes, cfg, state, equity, run_id):
    """Turn setups into exact plans using the rules that passed testing (research/results.json)."""
    s, plans = cfg["strategy"], []
    for st in setups:
        sym, price = st["symbol"], quotes[st["symbol"]]["price"]
        size = equity / s["max_positions"]
        plan = {"symbol": sym, "entry": price, "stop": price * (1 - s["stop_loss_pct"] / 100), "size_usd": round(size, 2),
                "risk_usd": round(size * s["stop_loss_pct"] / 100, 2), "exit": f"RSI > {s['rsi_exit']} or {s['max_hold_hours']}h"}
        plans.append(plan)
        log("Quant", sym, "plan", f"entry ~{price:.6g}, stop {plan['stop']:.6g} (-{s['stop_loss_pct']}%), size ${plan['size_usd']}, "
            f"risk ${plan['risk_usd']}, exit {plan['exit']}. Rule tested on held-out data: +0.5% over 36 trades (thin edge)",
            "Risk Manager: approve or reject", run_id)
    return plans


def risk_manager(plans, cfg, state, equity, closed, run_id):
    """Hard limits. Anything that breaks a rule is rejected, no exceptions."""
    R, approved = cfg["desk_rules"], []
    today = datetime.now(timezone.utc).date().isoformat()
    day = state.setdefault("day", {})
    if day.get("date") != today:
        day.update({"date": today, "start_equity": equity})
    day_loss_pct = 100 * (1 - equity / day["start_equity"]) if day["start_equity"] else 0
    streak = 0
    for t in reversed(closed):
        if t["pnl_usd"] > 0:
            break
        streak += 1
    pause_until = state.get("pause_until", 0)
    if streak >= R["max_consecutive_losses"] and not state.get("paused_for_streak") == len(closed):
        state["pause_until"] = pause_until = time.time() + R["pause_hours_after_streak"] * 3600
        state["paused_for_streak"] = len(closed)
        log("Risk Manager", "ALL", "pause", f"{streak} losing trades in a row", f"no new buys for {R['pause_hours_after_streak']}h", run_id)
    for p in plans:
        open_n = len(state["positions"]) + len(approved)
        reason = None
        if state.get("kill"):
            reason = "kill switch is on"
        elif time.time() < pause_until:
            reason = f"paused after {R['max_consecutive_losses']} straight losses"
        elif day_loss_pct >= R["max_daily_loss_pct"]:
            reason = f"down {day_loss_pct:.1f}% today (limit {R['max_daily_loss_pct']}%)"
        elif open_n >= R["max_open_positions"]:
            reason = f"already {open_n} positions (limit {R['max_open_positions']})"
        elif 100 * p["risk_usd"] / equity > R["max_risk_per_trade_pct"]:
            reason = f"risk {100 * p['risk_usd'] / equity:.1f}% of account (limit {R['max_risk_per_trade_pct']}%)"
        elif p["size_usd"] > state["cash_usd"]:
            reason = "not enough cash"
        if reason:
            log("Risk Manager", p["symbol"], "REJECTED", reason, "logged, no trade", run_id)
        else:
            approved.append(p)
            log("Risk Manager", p["symbol"], "APPROVED", f"risk {100 * p['risk_usd'] / equity:.1f}% of account, "
                f"{open_n + 1}/{R['max_open_positions']} positions, today {-day_loss_pct:+.1f}%", "Executor: place it", run_id)
    return approved


def executor(approved, quotes, broker, run_id):
    """Only acts on APPROVED plans. Never edits a plan, never adds size, never trades its own idea."""
    for p in approved:
        q = quotes[p["symbol"]]
        drift = 100 * (q["price"] / p["entry"] - 1)
        if drift > 1.0:
            log("Executor", p["symbol"], "skipped", f"price moved {drift:+.1f}% past the plan, not chasing", "back to Quant", run_id)
            continue
        broker.buy(p["symbol"], q)
        log("Executor", p["symbol"], "BOUGHT (paper)", f"${p['size_usd']} at ~{q['price']:.6g}, stop {p['stop']:.6g}", "monitor exits", run_id)


def desk_lead_brief(state, equity, closed, cfg, run_id):
    """Once a day (first run after 13:30 UTC = 7:30 AM ET): write the morning brief to data/brief.json."""
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("brief_date") == today or datetime.now(timezone.utc).hour * 60 + datetime.now(timezone.utc).minute < 13 * 60 + 30:
        return
    state["brief_date"] = today
    try:
        entries = [json.loads(l) for l in open(LOG) if l.strip()]
    except FileNotFoundError:
        entries = []
    since = time.time() - 86400
    recent = [e for e in entries if datetime.fromisoformat(e["t"]).timestamp() >= since]
    count = lambda seat, idea=None: sum(1 for e in recent if e["seat"] == seat and (idea is None or e["idea"] == idea))
    last24 = [t for t in closed if datetime.fromisoformat(t["t"]).timestamp() >= since]
    brief = {"date": today, "equity_usd": round(equity, 2), "change_pct": round(100 * (equity / state["start_usd"] - 1), 2),
             "pipeline_24h": {"setups": count("Chartist", "oversold dip"), "plans": count("Quant"), "approved": count("Risk Manager", "APPROVED"),
                              "rejected": count("Risk Manager", "REJECTED"), "bought": count("Executor", "BOUGHT (paper)")},
             "closed_24h": len(last24), "pnl_24h": round(sum(t["pnl_usd"] for t in last24), 2),
             "open_positions": list(state["positions"]), "watching": [e["ticker"] for e in recent if e["seat"] == "Chartist" and e["idea"] == "watch"][-5:],
             "rejections": [f"{e['ticker']}: {e['evidence']}" for e in recent if e["idea"] == "REJECTED"][-5:]}
    json.dump(brief, open(BRIEF, "w"), indent=1)
    log("Desk Lead", "ALL", "morning brief", f"{brief['pipeline_24h']['setups']} setups, {brief['pipeline_24h']['bought']} bought, "
        f"{brief['closed_24h']} closed ({brief['pnl_24h']:+.2f}) in 24h", "brief on dashboard", run_id)
