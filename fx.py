"""Rebound currencies: a separate $50 PAPER pot on 5 major currency pairs. Fake money only.

Honest note: in the Nov 18 test every currency strategy LOST money on unseen data. This runs the
least-bad one (RSI mean reversion, test result -0.9%) so you can watch it live. Expect it to struggle.

Rule (both directions, hourly candles, closed bars only):
  - RSI(14) crosses below 15 -> buy (bet it goes up);  RSI crosses above 85 -> short (bet it goes down)
    (moved from 20/80 on Nov 18: 15/85 lost less on unseen data, -0.41% vs -0.89%, still not profitable)
  - close when RSI crosses back past 50, price moves 1% against us, or after 24 hours
  - one position per pair, each 1/5 of equity, 0.02% cost per side assumed
"""
import json, os, sys, time
import stocks as S   # reuse fetch / rsi / now_iso

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
STATE = os.path.join(DATA, "fx_state.json")
STATUS = os.path.join(DATA, "fx_status.json")
PAIRS = {"USDJPY": "USDJPY=X", "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X"}
P = dict(start_usd=50.0, goal_usd=100.0, rsi_buy=15, rsi_short=85, rsi_exit=50, stop_pct=0.01, max_hold_bars=24,
         cost_per_side=0.0002, test_trades=50, kill_drawdown_pct=20, kill_after_trades=30)


def load():
    if os.path.exists(STATE): return json.load(open(STATE))
    return dict(cash=P["start_usd"], positions={}, trades=[], closed=0, wins=0, realized=0.0, fees=0.0,
                peak=P["start_usd"], equity_curve=[], last_signal={}, kill=None, notified=[])


def step():
    os.makedirs(DATA, exist_ok=True)
    s = load(); t = time.time(); info = {}
    for name, sym in PAIRS.items():
        v = S.fetch(sym); time.sleep(0.3)
        if not v: continue
        bars = [b for b in v["bars"] if b[0] <= t - 3600]           # closed hourly bars only
        closes = [b[4] for b in bars]
        prior = [b[4] for b in v["bars"] if b[0] <= t - 86400]
        info[name] = dict(bars=bars, rsi=S.rsi(closes), price=v["price"], prev=prior[-1] if prior else None,
                          fresh=bool(v["bars"]) and t - v["bars"][-1][0] < 2 * 3600)
    market_open = any(I["fresh"] for I in info.values())             # FX: Sun 5pm - Fri 5pm New York
    stopped = os.path.exists(os.path.join(DATA, "EMERGENCY_STOP")) or bool(s["kill"])

    def value(k, p):
        px = info.get(k, {"price": p["entry"]})["price"]
        return p["cost"] * (px / p["entry"] if p["dir"] == "long" else 2 - px / p["entry"])   # short = gains when price falls
    equity = lambda: s["cash"] + sum(value(k, p) for k, p in s["positions"].items())

    def close(k, why):
        p = s["positions"].pop(k); gross = value(k, p); fee = gross * P["cost_per_side"]; pnl = gross - fee - p["cost"]
        s["cash"] += gross - fee; s["fees"] += fee; s["realized"] += pnl; s["closed"] += 1; s["wins"] += pnl > 0
        s["trades"].append(dict(t=S.now_iso(t), side="sell" if p["dir"] == "long" else "buy", symbol=k, fill_price=info[k]["price"],
                                usd=round(gross, 2), fees_usd=round(fee, 4), pnl_usd=round(pnl, 4), pnl_pct=round(100 * pnl / p["cost"], 2),
                                reason=("close long " if p["dir"] == "long" else "close short ") + why))
        print(f"CLOSE {k} {p['dir']} {why} pnl {pnl:+.2f}")

    if market_open and not stopped:
        for k in list(s["positions"]):
            if k not in info: continue
            p = s["positions"][k]; I = info[k]; r = I["rsi"][-1]; px = I["price"]
            held = sum(1 for b in I["bars"] if b[0] >= p["entry_bar"])
            against = (px <= p["entry"] * (1 - P["stop_pct"])) if p["dir"] == "long" else (px >= p["entry"] * (1 + P["stop_pct"]))
            back = r is not None and ((p["dir"] == "long" and r > P["rsi_exit"]) or (p["dir"] == "short" and r < P["rsi_exit"]))
            if against: close(k, "stop")
            elif back: close(k, "rsi_back_to_50")
            elif held >= P["max_hold_bars"]: close(k, "time_limit")
        for k, I in info.items():
            r = I["rsi"]
            if k in s["positions"] or len(r) < 2 or r[-1] is None or r[-2] is None: continue
            bar_ts = I["bars"][-1][0]
            d = "long" if r[-1] < P["rsi_buy"] <= r[-2] else "short" if r[-1] > P["rsi_short"] >= r[-2] else None
            if not d or s["last_signal"].get(k) == bar_ts: continue
            s["last_signal"][k] = bar_ts
            if t - (bar_ts + 3600) > 75 * 60: continue
            usd = min(equity() / len(PAIRS), s["cash"] / (1 + P["cost_per_side"]))
            if usd < 1: continue
            fee = usd * P["cost_per_side"]; s["cash"] -= usd + fee; s["fees"] += fee
            s["positions"][k] = dict(dir=d, entry=I["price"], cost=usd, opened=S.now_iso(t), entry_bar=bar_ts + 3600)
            s["trades"].append(dict(t=S.now_iso(t), side="buy" if d == "long" else "sell", symbol=k, fill_price=I["price"],
                                    usd=round(usd, 2), fees_usd=round(fee, 4), reason=("open long" if d == "long" else "open short") + f", RSI {r[-1]:.0f}"))
            print(f"OPEN {k} {d} at {I['price']} RSI {r[-1]:.1f}")

    eq = equity(); s["peak"] = max(s["peak"], eq)
    if market_open:
        s["equity_curve"].append(dict(t=S.now_iso(t), equity=round(eq, 4))); s["equity_curve"] = s["equity_curve"][-3000:]
    s["trades"] = s["trades"][-400:]
    dd = 100 * (1 - eq / s["peak"])
    if not s["kill"]:
        if dd >= P["kill_drawdown_pct"]: s["kill"] = dict(t=S.now_iso(t), reason=f"currency pot fell {dd:.0f}% from its peak")
        elif s["closed"] >= P["kill_after_trades"] and s["realized"] <= 0: s["kill"] = dict(t=S.now_iso(t), reason=f"no profit after {s['closed']} currency trades")
        if s["kill"]: S.notify(s, "kill", "Rebound currencies stopped", "The currency paper pot stopped itself: " + s["kill"]["reason"])
    if s["closed"] >= P["test_trades"]:
        S.notify(s, "test", "Rebound currencies 50-trade test " + ("PASSED" if s["realized"] > 0 else "FAILED"),
                 f"{s['closed']} closed trades, P&L ${s['realized']:+.2f}, {s['wins']} wins.")
    json.dump(s, open(STATE, "w"), separators=(",", ":"))

    st = dict(updated=S.now_iso(t), kind="fx", buy_rsi=P["rsi_buy"], market_open=market_open, stopped=os.path.exists(os.path.join(DATA, "EMERGENCY_STOP")),
              kill=s["kill"], start_usd=P["start_usd"], goal_usd=P["goal_usd"], equity_usd=round(eq, 4), cash_usd=round(s["cash"], 4),
              change_pct=round(100 * (eq / P["start_usd"] - 1), 2), closed_trades=s["closed"], wins=s["wins"],
              realized_pnl_usd=round(s["realized"], 4), fees_usd=round(s["fees"], 4), params=P,
              positions=[dict(symbol=k, dir=p["dir"], entry=p["entry"], opened=p["opened"], cost_usd=round(p["cost"], 4), value_usd=round(value(k, p), 4),
                              pnl_pct=round(100 * (value(k, p) / p["cost"] - 1), 2),
                              stop_loss=round(p["entry"] * (1 - P["stop_pct"] if p["dir"] == "long" else 1 + P["stop_pct"]), 5))
                         for k, p in s["positions"].items()],
              trades=s["trades"][-60:][::-1], equity_curve=s["equity_curve"][-1500:],
              watchlist=[dict(symbol=k, price=I["price"], rsi=None if I["rsi"][-1] is None else round(I["rsi"][-1], 1),
                              change_24h=None if not I["prev"] else round(100 * (I["price"] / I["prev"] - 1), 2),
                              candles=[[b[0], round(b[1], 5), round(b[2], 5), round(b[3], 5), round(b[4], 5)] for b in I["bars"][-110:]],
                              holding=k in s["positions"]) for k, I in info.items()])
    json.dump(st, open(STATUS, "w"), separators=(",", ":"))
    print(f"fx: market {'OPEN' if market_open else 'closed'} | equity ${eq:.2f} | {len(s['positions'])} open | {s['closed']} closed | {len(info)}/5 quotes")


def report():
    if not os.path.exists(STATUS): print("no fx data yet"); return
    d = json.load(open(STATUS))
    print(f"CURRENCIES (paper) ${d['equity_usd']:.2f} ({d['change_pct']:+.2f}%) | trades {d['closed_trades']}")
    for p in d["positions"]: print(f"  {p['dir']} {p['symbol']} {p['pnl_pct']:+.2f}%")


if __name__ == "__main__":
    report() if sys.argv[1:] == ["report"] else step()
