"""Dipwatch stocks: a separate PAPER account that dip-buys big US stocks.

Strategy (the one that passed the Nov 18 backtest on unseen data, see research/README.md):
  - hourly candles, RSI(14) on CLOSED bars
  - buy when RSI crosses below 15
  - sell when RSI > 50, price falls 4% below entry, or after 48 market hours
  - max 6 positions, each 1/6 of equity, 0.03% cost per side assumed
Fake money only. There is no live mode in this file.

Usage: python3 stocks.py           (one check; trades only while the US market is open)
       python3 stocks.py report
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
STATE = os.path.join(DATA, "stocks_state.json")
STATUS = os.path.join(DATA, "stocks_status.json")
STOPFILE = os.path.join(DATA, "EMERGENCY_STOP")
NOTIFY = os.path.join(DATA, "notify.md")

SYMBOLS = ("SPY QQQ AAPL MSFT NVDA TSLA AMZN META GOOGL AMD NFLX PLTR COIN JPM XOM "   # first 15 (Nov 18 test)
           "AVGO ORCL CRM ADBE INTC QCOM MU UBER SHOP SNOW CRWD PANW ABNB PYPL HOOD SOFI RIVN F GM DIS NKE SBUX "
           "MCD WMT COST TGT HD LOW KO PEP PG JNJ PFE MRK LLY UNH BAC WFC GS MS V MA CVX BA CAT GE IWM DIA SMH").split()  # +48, checked on unseen data first
P = dict(start_usd=50.0, goal_usd=100.0, rsi_buy=15, rsi_sell=50, stop_pct=0.04, max_hold_bars=48,
         max_positions=6, cost_per_side=0.0003, test_trades=50,
         kill_drawdown_pct=20, kill_after_trades=30)


def now_iso(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n: return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(c)):
        if i > n:
            ag = (ag * (n - 1) + g[i - 1]) / n; al = (al * (n - 1) + l[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def fetch(sym):
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1h&range=30d"
    for wait in (0, 5, 15):
        time.sleep(wait)
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
            r = r["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            bars = [(t, o, h, l, c) for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]
            m = r["meta"]; reg = m["currentTradingPeriod"]["regular"]
            return dict(bars=bars, price=m.get("regularMarketPrice") or bars[-1][4], prev=m.get("chartPreviousClose"),
                        open_=reg["start"] <= time.time() < reg["end"])
        except Exception as e:
            err = e
    print(f"{sym}: fetch failed ({err})")
    return None


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return dict(cash=P["start_usd"], positions={}, trades=[], closed=0, wins=0, realized=0.0, fees=0.0,
                peak=P["start_usd"], equity_curve=[], last_signal={}, kill=None, notified=[])


def notify(s, key, title, body):
    if key in s["notified"]: return
    s["notified"].append(key)
    with open(NOTIFY, "a") as f:
        f.write(("" if os.path.exists(NOTIFY) and os.path.getsize(NOTIFY) else title + "\n") + body + "\n")


def step():
    os.makedirs(DATA, exist_ok=True)
    s = load(); t = time.time(); q = {}
    for sym in SYMBOLS:
        q[sym] = fetch(sym); time.sleep(0.25)
    ok = {k: v for k, v in q.items() if v}
    market_open = any(v["open_"] for v in ok.values())
    stopped = os.path.exists(STOPFILE) or bool(s["kill"])

    # closed bars only: while the market is open, the newest hourly bar is still forming
    info = {}
    for sym, v in ok.items():
        bars = [b for b in v["bars"] if not (market_open and b[0] > t - 3600)]
        closes = [b[4] for b in bars]; r = rsi(closes)
        info[sym] = dict(bars=bars, closes=closes, rsi=r, price=v["price"], prev=v["prev"])

    def equity():
        return s["cash"] + sum(p["qty"] * info.get(k, {"price": p["entry"]})["price"] for k, p in s["positions"].items())

    def sell(sym, why):
        p = s["positions"].pop(sym); px = info[sym]["price"]; gross = p["qty"] * px; fee = gross * P["cost_per_side"]
        pnl = gross - fee - p["cost"]; s["cash"] += gross - fee; s["fees"] += fee; s["realized"] += pnl
        s["closed"] += 1; s["wins"] += pnl > 0
        s["trades"].append(dict(t=now_iso(t), side="sell", symbol=sym, fill_price=px, usd=round(gross, 2), fees_usd=round(fee, 4),
                                pnl_usd=round(pnl, 4), pnl_pct=round(100 * pnl / p["cost"], 2), reason=why))
        print(f"SELL {sym} {why} pnl {pnl:+.2f}")

    if market_open and not stopped:
        # exits first
        for sym in list(s["positions"]):
            if sym not in info: continue
            p = s["positions"][sym]; I = info[sym]
            held = sum(1 for b in I["bars"] if b[0] >= p["entry_bar"])
            if I["price"] <= p["entry"] * (1 - P["stop_pct"]): sell(sym, "stop_loss")
            elif I["rsi"][-1] is not None and I["rsi"][-1] > P["rsi_sell"]: sell(sym, "rsi_recovered")
            elif held >= P["max_hold_bars"]: sell(sym, "time_limit")
        # entries: RSI crossed below the line on the newest closed bar, bar closed within the last 75 min
        for sym, I in info.items():
            r = I["rsi"]
            if sym in s["positions"] or len(r) < 2 or r[-1] is None or r[-2] is None: continue
            bar_ts = I["bars"][-1][0]
            if not (r[-1] < P["rsi_buy"] <= r[-2]) or s["last_signal"].get(sym) == bar_ts: continue
            s["last_signal"][sym] = bar_ts
            if t - (bar_ts + 3600) > 75 * 60 or len(s["positions"]) >= P["max_positions"]: continue
            usd = min(equity() / P["max_positions"], s["cash"] / (1 + P["cost_per_side"]))
            if usd < 1: continue
            fee = usd * P["cost_per_side"]; px = I["price"]
            s["cash"] -= usd + fee; s["fees"] += fee
            s["positions"][sym] = dict(qty=usd / px, entry=px, cost=usd + fee, opened=now_iso(t), entry_bar=bar_ts + 3600, rsi=round(r[-1], 1))
            s["trades"].append(dict(t=now_iso(t), side="buy", symbol=sym, fill_price=px, usd=round(usd, 2), fees_usd=round(fee, 4), reason=f"RSI {r[-1]:.0f}"))
            print(f"BUY {sym} at {px} RSI {r[-1]:.1f}")

    eq = equity(); s["peak"] = max(s["peak"], eq)
    if market_open:
        s["equity_curve"].append(dict(t=now_iso(t), equity=round(eq, 4))); s["equity_curve"] = s["equity_curve"][-3000:]
    s["trades"] = s["trades"][-400:]

    # kill rules + milestones
    dd = 100 * (1 - eq / s["peak"])
    if not s["kill"]:
        if dd >= P["kill_drawdown_pct"]:
            s["kill"] = dict(t=now_iso(t), reason=f"stocks account fell {dd:.0f}% from its peak")
        elif s["closed"] >= P["kill_after_trades"] and s["realized"] <= 0:
            s["kill"] = dict(t=now_iso(t), reason=f"no profit after {s['closed']} stock trades")
        if s["kill"]:
            notify(s, "kill", "Dipwatch stocks stopped", "The stocks paper account stopped itself: " + s["kill"]["reason"])
    if eq >= P["goal_usd"]:
        notify(s, "goal", "Dipwatch stocks hit $100 (paper)", f"Stocks paper account reached ${eq:.2f}.")
    if s["closed"] >= P["test_trades"]:
        verdict = "PASSED" if s["realized"] > 0 else "FAILED"
        notify(s, "test", f"Dipwatch stocks 50-trade test {verdict}", f"{s['closed']} closed trades, P&L ${s['realized']:+.2f}, {s['wins']} wins.")

    json.dump(s, open(STATE, "w"), separators=(",", ":"))

    status = dict(updated=now_iso(t), market_open=market_open, stopped=os.path.exists(STOPFILE), kill=s["kill"],
                  start_usd=P["start_usd"], goal_usd=P["goal_usd"], equity_usd=round(eq, 4), cash_usd=round(s["cash"], 4),
                  change_pct=round(100 * (eq / P["start_usd"] - 1), 2), closed_trades=s["closed"], wins=s["wins"],
                  realized_pnl_usd=round(s["realized"], 4), fees_usd=round(s["fees"], 4), params=P,
                  positions=[dict(symbol=k, entry=p["entry"], opened=p["opened"], cost_usd=round(p["cost"], 4),
                                  value_usd=round(p["qty"] * info.get(k, {"price": p["entry"]})["price"], 4),
                                  pnl_pct=round(100 * (p["qty"] * info.get(k, {"price": p["entry"]})["price"] / p["cost"] - 1), 2))
                             for k, p in s["positions"].items()],
                  trades=s["trades"][-60:][::-1], equity_curve=s["equity_curve"][-1500:],
                  watchlist=[dict(symbol=k, price=I["price"], rsi=None if I["rsi"][-1] is None else round(I["rsi"][-1], 1),
                                  change_day=None if not I["prev"] else round(100 * (I["price"] / I["prev"] - 1), 2),
                                  closes=[round(c, 4) for c in I["closes"][-48:]], holding=k in s["positions"])
                             for k, I in info.items()])
    json.dump(status, open(STATUS, "w"), separators=(",", ":"))
    print(f"stocks: market {'OPEN' if market_open else 'closed'} | equity ${eq:.2f} | {len(s['positions'])} open | {s['closed']} closed | {len(ok)}/{len(SYMBOLS)} quotes")


def report():
    if not os.path.exists(STATUS): print("no stocks data yet"); return
    d = json.load(open(STATUS))
    print(f"STOCKS (paper) ${d['equity_usd']:.2f} ({d['change_pct']:+.2f}%) | trades {d['closed_trades']} | market {'open' if d['market_open'] else 'closed'}")
    for p in d["positions"]: print(f"  holding {p['symbol']} {p['pnl_pct']:+.2f}%")
    near = sorted([w for w in d["watchlist"] if w["rsi"] is not None], key=lambda w: w["rsi"])[:3]
    print("  closest to buy: " + ", ".join(f"{w['symbol']} RSI {w['rsi']:.0f}" for w in near))


if __name__ == "__main__":
    report() if sys.argv[1:] == ["report"] else step()
