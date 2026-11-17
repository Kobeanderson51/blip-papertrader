"""Backtest candidate strategies on hourly Solana meme coin candles.

Rules that keep this honest:
- Signals use only data up to the CLOSE of bar t; trades fill at the OPEN of bar t+1 (no look-ahead).
- Every fill pays swap fee + slippage per side. Base case 0.55%/side; stress case 2x.
- Time split: first 60% of hours = TRAIN (pick parameters), last 40% = TEST (judge). Never random.
- Every parameter combo tried is counted and reported.
- Portfolio = 10 coins, equal capital each, flat when not in a trade (cash earns 0).
"""
import json, glob, itertools, os
import numpy as np
import pandas as pd

COST = 0.0055          # per side: 0.25% swap fee + 0.30% slippage (estimate for ~$5 orders in $1M+ pools, plus spread)
TRAIN_FRAC = 0.6
HOURS_PER_YEAR = 8760


def load():
    out = {}
    for p in sorted(glob.glob("data/*.json")):
        d = pd.DataFrame(json.load(open(p)), columns=["t", "o", "h", "l", "c", "v"]).set_index("t")
        d.index = pd.to_datetime(d.index, unit="s", utc=True)
        d = d[~d.index.duplicated()].sort_index().asfreq("1h")
        d["c"] = d["c"].ffill(); d["o"] = d["o"].fillna(d["c"]); d["h"] = d["h"].fillna(d["c"]); d["l"] = d["l"].fillna(d["c"]); d["v"] = d["v"].fillna(0)
        out[os.path.basename(p)[:-5]] = d
    return out


def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(d, n=14):
    tr = pd.concat([d.h - d.l, (d.h - d.c.shift()).abs(), (d.l - d.c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def simulate(d, entry, exit_rule, cost):
    """entry: bool Series known at bar close. exit_rule(i, entry_px, peak, bars_held) -> exit price or None (checked on bar i using its OHLC).
    Returns hourly strategy returns (Series) and list of trade returns."""
    o, h, l, c = d.o.values, d.h.values, d.l.values, d.c.values
    en = entry.values
    n = len(c); ret = np.zeros(n); trades = []
    pos = False; px = peak = 0.0; held = 0
    for i in range(1, n):
        if not pos:
            if en[i - 1]:                       # signal at close of i-1 -> buy open of i
                pos, px, peak, held = True, o[i], o[i], 0
                ret[i] = (c[i] / o[i]) * (1 - cost) - 1
                prev = c[i]
                # same-bar exit check
                ex = exit_rule(i, px, max(peak, h[i]), held)
                peak = max(peak, h[i])
                if ex is not None:
                    ret[i] = (ex / o[i]) * (1 - cost) * (1 - cost) - 1
                    trades.append((i, (ex / px) * (1 - cost) ** 2 - 1)); pos = False
            continue
        held += 1
        ex = exit_rule(i, px, max(peak, h[i]), held)
        if ex is not None:
            ret[i] = (ex / prev) * (1 - cost) - 1
            trades.append((i, (ex / px) * (1 - cost) ** 2 - 1)); pos = False
        else:
            ret[i] = c[i] / prev - 1
            prev = c[i]; peak = max(peak, h[i])
    return pd.Series(ret, index=d.index), trades


# ---------------------------------------------------------------- strategies
def strat_ema(d, fast, slow, tp, sl, hold):
    f, s = ema(d.c, fast), ema(d.c, slow)
    entry = (f > s) & (f.shift() <= s.shift())
    def ex(i, px, peak, held):
        if d.l.values[i] <= px * (1 - sl): return min(d.o.values[i], px * (1 - sl))
        if d.h.values[i] >= px * (1 + tp): return max(d.o.values[i], px * (1 + tp))
        if held >= hold: return d.c.values[i]
    return entry, ex


def strat_breakout(d, n, k, hold):
    hh = d.h.rolling(n).max().shift(1)
    a = atr(d)
    entry = d.c > hh
    av = a.values
    def ex(i, px, peak, held):
        stop = peak - k * av[i - 1]
        if d.l.values[i] <= stop: return min(d.o.values[i], stop)
        if held >= hold: return d.c.values[i]
    return entry, ex


def strat_meanrev(d, thr, sl, hold):
    r = rsi(d.c); rv = r.values
    entry = (r < thr) & (r.shift() >= thr)
    def ex(i, px, peak, held):
        if d.l.values[i] <= px * (1 - sl): return min(d.o.values[i], px * (1 - sl))
        if rv[i] > 50 or held >= hold: return d.c.values[i]
    return entry, ex


def strat_score(d, thr, rr, k):
    """Multi-factor 'confluence score' like the TradingView post: 5 factors x 20 pts, fire when score >= thr on a closed bar."""
    e50, e200 = ema(d.c, 50), ema(d.c, 200)
    r = rsi(d.c); a = atr(d); vavg = d.v.rolling(24).mean()
    rng = (d.h - d.l).replace(0, np.nan)
    score = 20 * ((e50 > e200).astype(int) + (d.c > e50).astype(int) + ((r > 55) & (r < 75)).astype(int)
                  + (d.v > 1.5 * vavg).astype(int) + (((d.c - d.l) / rng) > 0.7).astype(int))
    entry = (score >= thr) & (score.shift() < thr)
    av = a.values
    state = {}
    def ex(i, px, peak, held):
        if held == 0: state["stop"] = px - k * av[i - 1]; state["tgt"] = px + rr * k * av[i - 1]
        if d.l.values[i] <= state["stop"]: return min(d.o.values[i], state["stop"])
        if d.h.values[i] >= state["tgt"]: return max(d.o.values[i], state["tgt"])
        if held >= 48: return d.c.values[i]
    return entry, ex


GRIDS = {
    "EMA momentum + TP/SL (current bot)": (strat_ema, dict(fast=[4, 8, 12], slow=[16, 24, 48], tp=[0.03, 0.05, 0.08], sl=[0.02, 0.03, 0.05], hold=[12, 48])),
    "Breakout + ATR trailing stop": (strat_breakout, dict(n=[24, 48, 96, 168], k=[2.0, 3.0, 4.0], hold=[48, 168])),
    "RSI mean reversion": (strat_meanrev, dict(thr=[20, 25, 30], sl=[0.05, 0.08, 0.12], hold=[12, 24, 48])),
    "Confluence score >= threshold (TradingView-post style)": (strat_score, dict(thr=[60, 80, 100], rr=[1.5, 2.0, 3.0], k=[1.0, 1.5, 2.0])),
}


def metrics(r, trades):
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    sd = r.std()
    sharpe = (r.mean() / sd * np.sqrt(HOURS_PER_YEAR)) if sd > 0 else 0.0
    t = np.array(trades) if trades else np.array([0.0])
    wins, losses = t[t > 0], t[t <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else np.inf
    return dict(ret=eq.iloc[-1] - 1, sharpe=sharpe, maxdd=dd, trades=len(trades), win=(len(wins) / len(trades)) if trades else 0, pf=pf)


_CACHE = {}


def run_portfolio(data, fn, params, cost, part):
    key = (fn.__name__, tuple(sorted(params.items())), cost)
    if key not in _CACHE:
        sims = []
        for sym, d in data.items():
            entry, ex = fn(d, **params)
            r, tr = simulate(d, entry, ex, cost)
            sims.append((d, r, tr))
        _CACHE[key] = sims
    rets, trades = [], []
    for d, r, tr in _CACHE[key]:
        k = int(len(d) * TRAIN_FRAC)
        rets.append(r.iloc[:k] if part == "train" else r.iloc[k:])
        trades += [x for i, x in tr if (i < k) == (part == "train")]
    port = pd.concat(rets, axis=1).fillna(0).mean(axis=1)
    return metrics(port, trades)


def main():
    data = load()
    rows, tried = [], 0
    for name, (fn, grid) in GRIDS.items():
        keys = list(grid)
        results = []
        for combo in itertools.product(*grid.values()):
            p = dict(zip(keys, combo)); tried += 1
            tr = run_portfolio(data, fn, p, COST, "train")
            results.append((p, tr))
        best_p, best_tr = max(results, key=lambda x: x[1]["sharpe"])
        te = run_portfolio(data, fn, best_p, COST, "test")
        te2 = run_portfolio(data, fn, best_p, 2 * COST, "test")
        # robustness: share of all combos that are profitable on TEST
        prof = sum(1 for p, _ in results if run_portfolio(data, fn, p, COST, "test")["ret"] > 0) / len(results)
        rows.append(dict(strategy=name, best_params=best_p, combos=len(results), train=best_tr, test=te, test_2x_cost=te2, share_of_combos_profitable_on_test=prof))
        print(name, "done", flush=True)
    # benchmark: buy & hold equal weight
    bh = {}
    for part in ("train", "test"):
        rets = []
        for d in data.values():
            cut = d.index[int(len(d) * TRAIN_FRAC)]
            r = d.c.pct_change().fillna(0)
            rets.append(r[r.index < cut] if part == "train" else r[r.index >= cut])
        bh[part] = metrics(pd.concat(rets, axis=1).fillna(0).mean(axis=1), [])
    cut = list(data.values())[0].index[int(len(list(data.values())[0]) * TRAIN_FRAC)]
    json.dump(dict(rows=rows, buy_hold=bh, variants_tried=tried, cost_per_side=COST, split=str(cut),
                   start=str(list(data.values())[0].index[0]), end=str(list(data.values())[0].index[-1]), coins=list(data)),
              open("results.json", "w"), indent=1, default=float)
    print("variants tried:", tried, "split at", cut)


if __name__ == "__main__":
    main()
